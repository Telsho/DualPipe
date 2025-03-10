import time
from typing import Tuple, List, Union, Callable, Optional

import torch
import torch.nn as nn
import torch.distributed as dist

import dualpipe.comm as comm
from dualpipe.utils import WeightGradStore, run_backward, scatter, gather

import logging
import sys
logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s %(levelname)s %(message)s",
    stream=sys.stdout,
    force=True
)

class DualPipe(nn.Module):
    def __init__(
        self,
        modules: Tuple[nn.Module, nn.Module],
        batch_dim: int = 0,
        process_group: Optional[dist.ProcessGroup] = None,
        rank_mapping: Optional[List[int]] = None,
    ) -> None:
        super().__init__()

        assert next(modules[0].parameters()).device == torch.device(torch.cuda.current_device())
        self.module = nn.ModuleList(modules)
        self.overlaped_forward_backward = type(modules[0]) == type(modules[1]) and hasattr(type(modules[0]), "overlaped_forward_backward")
        self.batch_dim = batch_dim
        self.group = process_group or dist.distributed_c10d._get_default_group()
        self.num_ranks = self.group.size()

        # rank_mapping: Map rank in process_group to actual pp rank.
        # rank_inverse_mapping: Map actual pp rank to rank in process_group.
        if rank_mapping is None:
            rank_mapping = list(range(self.num_ranks))
        rank_inverse_mapping = [None] * (self.num_ranks + 1)
        for i in range(self.num_ranks):
            rank_inverse_mapping[rank_mapping[i]] = i

        self.rank = rank_mapping[self.group.rank()]
        self.first_rank = rank_inverse_mapping[0]
        self.prev_rank = rank_inverse_mapping[self.rank - 1]
        self.next_rank = rank_inverse_mapping[self.rank + 1]
        self.last_rank = rank_inverse_mapping[self.num_ranks - 1]

        self.is_first_rank = self.rank == 0
        self.is_last_rank = self.rank == self.num_ranks - 1
        self.is_in_second_half = self.rank >= self.num_ranks // 2
        # Only mark middle ranks when we have more than 2 total ranks
        self.is_middle_rank = self.num_ranks > 2 and ((self.rank == self.num_ranks // 2 - 1) or (self.rank == self.num_ranks // 2))

    def _reset_states(self) -> None:
        WeightGradStore.clear()

        self.input_chunks: Tuple[List[List[torch.Tensor]], List[List[torch.Tensor]]] = ([], [])
        self.output_chunks: Tuple[List[List[torch.Tensor]], List[List[torch.Tensor]]] = ([], [])
        self.input_grad_chunks: Tuple[List[List[torch.Tensor]], List[List[torch.Tensor]]] = ([], [])
        self.output_grad_chunks: Tuple[List[List[torch.Tensor]], List[List[torch.Tensor]]] = ([], [])
        self.labels: Tuple[List[List[torch.Tensor]], List[List[torch.Tensor]]] = None
        self.loss_chunks: List[torch.Tensor] = []
        self.criterion: Callable = None

        self.current_f_chunk_id: List[int] = [0, 0]
        self.current_b_chunk_id: List[int] = [0, 0]
        self.current_send_f_chunk_id: List[int] = [0, 0]
        self.current_send_b_chunk_id: List[int] = [0, 0]
        self.current_recv_f_chunk_id: List[int] = [0, 0]
        self.current_recv_b_chunk_id: List[int] = [0, 0]
        self.comm_ops: List[dist.P2POp] = []
        self.to_free: List[torch.Tensor] = []

    def _forward_compute_chunk(self, phase: int) -> None:
        logger = logging.getLogger(__name__)
        start_time = time.time()
        logger.debug(f"Rank {self.rank} - _forward_compute_chunk phase={phase} starting at {start_time}")
        
        phase ^= self.is_in_second_half
        chunk_id = self.current_f_chunk_id[phase]
        self.current_f_chunk_id[phase] += 1
        
        logger.debug(f"Rank {self.rank} - Getting inputs from chunk_id={chunk_id}")
        inputs = self.input_chunks[phase][chunk_id]
        
        if self.forward_only:
            self.input_chunks[phase][chunk_id] = None

        is_last_stage = (self.is_first_rank and phase == 1) or (self.is_last_rank and phase == 0)
        logger.debug(f"Rank {self.rank} - _forward_compute_chunk: phase={phase}, is_first_rank={self.is_first_rank}, is_last_rank={self.is_last_rank}, is_last_stage={is_last_stage}")
    

        logger.debug(f"Rank {self.rank} - About to call module[{phase}] at {time.time()}")
        try:
            outputs = self.module[phase](*inputs)
            logger.debug(f"Rank {self.rank} - Module call completed at {time.time()}")
        except Exception as e:
            logger.error(f"Rank {self.rank} - Exception in module call: {str(e)}")
            import traceback
            logger.error(traceback.format_exc())
            raise
        outputs = [outputs] if isinstance(outputs, torch.Tensor) else outputs
        if is_last_stage and self.criterion is not None:
            labels = self.labels[phase][chunk_id]
            loss = self.criterion(*outputs, *labels)
            self.loss_chunks.append(loss)

        if (not is_last_stage) or self.return_outputs:
            self.output_chunks[phase].append(outputs)

    def _backward_compute_chunk(self, phase: int, enable_zb: bool = False) -> None:
        if self.forward_only:
            return

        phase ^= self.is_in_second_half
        chunk_id = self.current_b_chunk_id[phase]
        self.current_b_chunk_id[phase] += 1

        is_last_stage = (self.is_first_rank and phase == 1) or (self.is_last_rank and phase == 0)

        WeightGradStore.enabled = enable_zb
        if is_last_stage:
            loss = self.loss_chunks[chunk_id]
            loss.backward()
            loss.detach_()
        else:
            outputs = self.output_chunks[phase][chunk_id]
            if not self.return_outputs:
                self.output_chunks[phase][chunk_id] = None
            output_grads = self.output_grad_chunks[phase][chunk_id]
            self.output_grad_chunks[phase][chunk_id] = None
            non_empty = [(t, g) for t, g in zip(outputs, output_grads) if g is not None]
            outputs, output_grads = list(zip(*non_empty))
            if len(outputs) > 0:
                run_backward(outputs, output_grads)
        WeightGradStore.enabled = False
        if enable_zb:
            WeightGradStore.flush()

        inputs = self.input_chunks[phase][chunk_id]
        self.input_chunks[phase][chunk_id] = None
        input_grads = [t.grad for t in inputs]
        self.input_grad_chunks[phase].append(input_grads)

    def _forward_backward_compute_chunk(self, phase0: int, phase1: int) -> None:
        if self.forward_only:
            self._forward_compute_chunk(phase0)
            return

        if not self.overlaped_forward_backward:
            self._forward_compute_chunk(phase0)
            self._backward_compute_chunk(phase1)
            return

        # pre-forward
        phase0 ^= self.is_in_second_half
        chunk_id0 = self.current_f_chunk_id[phase0]
        self.current_f_chunk_id[phase0] += 1
        module0 = self.module[phase0]
        inputs0 = self.input_chunks[phase0][chunk_id0]
        is_last_stage0 = (self.is_first_rank and phase0 == 1) or (self.is_last_rank and phase0 == 0)

        if is_last_stage0 and self.criterion is not None:
            labels0 = self.labels[phase0][chunk_id0]
            criterion0 = self.criterion
        else:
            labels0 = []
            criterion0 = None

        # pre-backward
        phase1 ^= self.is_in_second_half
        chunk_id1 = self.current_b_chunk_id[phase1]
        self.current_b_chunk_id[phase1] += 1
        module1 = self.module[phase1]
        is_last_stage1 = (self.is_first_rank and phase1 == 1) or (self.is_last_rank and phase1 == 0)

        if is_last_stage1:
            loss1 = self.loss_chunks[chunk_id1]
            outputs1 = []
            output_grads1 = []
        else:
            loss1 = None
            outputs1 = self.output_chunks[phase1][chunk_id1]
            if not self.return_outputs:
                self.output_chunks[phase1][chunk_id1] = None
            output_grads1 = self.output_grad_chunks[phase1][chunk_id1]
            self.output_grad_chunks[phase1][chunk_id1] = None
            non_empty = [(t, g) for t, g in zip(outputs1, output_grads1) if g is not None]
            outputs1, output_grads1 = list(zip(*non_empty))

        # forward & backward
        outputs0, loss0 = type(module0).overlaped_forward_backward(
            module0, inputs0, criterion0, labels0,
            module1, loss1, outputs1, output_grads1,
        )

        # post-forward
        if (not is_last_stage0) or self.return_outputs:
            self.output_chunks[phase0].append(outputs0)
        if is_last_stage0 and self.criterion is not None:
            self.loss_chunks.append(loss0)

        # post-backward
        inputs = self.input_chunks[phase1][chunk_id1]
        self.input_chunks[phase1][chunk_id1] = None
        input_grads1 = [t.grad for t in inputs]
        self.input_grad_chunks[phase1].append(input_grads1)

    def _forward_chunk(self, phase: int, recv: bool = True, send: bool = True) -> None:
        logger = logging.getLogger(__name__)
        start_time = time.time()
        logger.debug(f"Rank {self.rank} - _forward_chunk phase={phase}, recv={recv}, send={send} starting at {start_time}")
        
        if recv:
            logger.debug(f"Rank {self.rank} - _forward_chunk phase={phase}: Calling self._recv_forward({phase})")
            self._recv_forward(phase)
            
        logger.debug(f"Rank {self.rank} - _forward_chunk phase={phase}: Calling self._commit_and_wait_comm()")
        self._commit_and_wait_comm()
        
        logger.debug(f"Rank {self.rank} - _forward_chunk phase={phase}: Calling self._forward_compute_chunk({phase})")
        self._forward_compute_chunk(phase)
        
        if send:
            logger.debug(f"Rank {self.rank} - _forward_chunk phase={phase}: Calling self._send_forward({phase})")
            self._send_forward(phase)
            
        logger.debug(f"Rank {self.rank} - _forward_chunk phase={phase} completed at {time.time()}, took {time.time()-start_time:.3f}s")

    def _recv_forward(self, phase: int) -> None:
        logger = logging.getLogger(__name__)
        start_time = time.time()
        logger.debug(f"Rank {self.rank} - _recv_forward phase={phase} starting at {start_time}")
        
        phase ^= self.is_in_second_half
        is_first_stage = (self.is_first_rank and phase == 0) or (self.is_last_rank and phase == 1)
        if is_first_stage:
            logger.debug(f"Rank {self.rank} - _recv_forward phase={phase}: is_first_stage=True, returning early")
            return

        self.current_recv_f_chunk_id[phase] += 1
        prev_or_next = self.prev_rank if phase == 0 else self.next_rank
        logger.debug(f"Rank {self.rank} - _recv_forward phase={phase}: Calling comm.append_irecv with prev_or_next={prev_or_next}")
        tensors = comm.append_irecv(self.comm_ops, prev_or_next, self.group)
        self.input_chunks[phase].append(tensors)
        
        logger.debug(f"Rank {self.rank} - _recv_forward phase={phase} completed at {time.time()}, took {time.time()-start_time:.3f}s")

    def _backward_chunk(self, phase: int, enable_zb: bool = False, recv: bool = True, send: bool = True) -> None:
        if recv:
            self._recv_backward(phase)
        self._commit_and_wait_comm()

        self._backward_compute_chunk(phase, enable_zb)

        if send:
            self._send_backward(phase)
            
    def _forward_backward_chunk(self, phase0: int, phase1: int, recv0: bool = True) -> None:
        logger = logging.getLogger(__name__)
        logger.propagate = True

        logger.debug(f"Rank {self.rank} - Starting _forward_backward_chunk phases {phase0}, {phase1} at {time.time()}")
        if recv0:
            logger.debug(f"Rank {self.rank} - Receiving forward phase {phase0} at {time.time()}")
            self._recv_forward(phase0)
        
        logger.debug(f"Rank {self.rank} - Receiving backward phase {phase1} at {time.time()}")
        self._recv_backward(phase1)
        
        logger.debug(f"Rank {self.rank} - Committing and waiting for comm at {time.time()}")
        self._commit_and_wait_comm()

        logger.debug(f"Rank {self.rank} - Computing forward_backward at {time.time()}")
        self._forward_backward_compute_chunk(phase0, phase1)

        logger.debug(f"Rank {self.rank} - Sending forward phase {phase0} at {time.time()}")
        self._send_forward(phase0)
        
        logger.debug(f"Rank {self.rank} - Sending backward phase {phase1} at {time.time()}")
        self._send_backward(phase1)
        
        logger.debug(f"Rank {self.rank} - Completed _forward_backward_chunk at {time.time()}")

    def _weight_chunk(self) -> None:
        if self.forward_only:
            return

        self._commit_and_wait_comm()

        # Assume FIFO
        WeightGradStore.pop()

    def _free_tensors(self) -> None:
        for tensor in self.to_free:
            assert tensor._base is None, f"pipeline stage should not return view tensors {dist.get_rank(), tensor.shape}"
            tensor.data = torch.Tensor()
        self.to_free = []

    def _send_forward(self, phase: int) -> None:
        phase ^= self.is_in_second_half
        is_last_stage = (self.is_first_rank and phase == 1) or (self.is_last_rank and phase == 0)
        if is_last_stage:
            return

        chunk_id = self.current_send_f_chunk_id[phase]
        self.current_send_f_chunk_id[phase] += 1
        tensors = self.output_chunks[phase][chunk_id]

        comm.append_isend(self.comm_ops, tensors, self.next_rank if phase == 0 else self.prev_rank, self.group)

        if not self.return_outputs:
            self.to_free.extend(tensors)

    def _recv_backward(self, phase: int) -> None:
        if self.forward_only:
            return

        phase ^= self.is_in_second_half
        is_last_stage = (self.is_first_rank and phase == 1) or (self.is_last_rank and phase == 0)
        if is_last_stage:
            return

        self.current_recv_b_chunk_id[phase] += 1
        tensors = comm.append_irecv(self.comm_ops, self.next_rank if phase == 0 else self.prev_rank, self.group)
        self.output_grad_chunks[phase].append(tensors)

    def _send_backward(self, phase: int) -> None:
        if self.forward_only:
            return

        phase ^= self.is_in_second_half
        is_first_stage = (self.is_first_rank and phase == 0) or (self.is_last_rank and phase == 1)
        if is_first_stage:
            return

        chunk_id = self.current_send_b_chunk_id[phase]
        self.current_send_b_chunk_id[phase] += 1
        tensors = self.input_grad_chunks[phase][chunk_id]
        self.input_grad_chunks[phase][chunk_id] = None

        comm.append_isend(self.comm_ops, tensors, self.prev_rank if phase == 0 else self.next_rank, self.group)

    def _commit_and_wait_comm(self) -> None:
        if not self.comm_ops:
            return
        reqs = dist.batch_isend_irecv(self.comm_ops)
        for req in reqs:
            req.wait()
        self.comm_ops = []
        self._free_tensors()

    def step(
        self,
        *inputs: Optional[torch.Tensor],
        num_chunks: int = 0,
        criterion: Optional[Callable] = None,
        labels: List[Optional[torch.Tensor]] = [],
        return_outputs: bool = False,
    ) -> Tuple[Optional[torch.Tensor], Optional[Union[torch.Tensor, Tuple[torch.Tensor]]]]:
        """
        Execute a training or inference step.
        """
        logger = logging.getLogger(__name__)
        logger.propagate = True
        logger.info(f"Step start: rank {self.rank}/{self.num_ranks}, num_chunks={num_chunks}, return_outputs={return_outputs}")
        
        assert comm.TENSOR_SHAPES is not None and comm.TENSOR_DTYPE is not None, \
            "You need to call set_p2p_tensor_shapes and set_p2p_tensor_dtype before doing a step."
        self.forward_only = not torch.is_grad_enabled()
        self.return_outputs = return_outputs

        rank = self.rank
        num_ranks = self.num_ranks
        assert num_ranks % 2 == 0, "Number of ranks must be even"
        assert num_chunks > 0 and num_chunks % 2 == 0 and num_chunks >= num_ranks * 2, f"{num_chunks=}, {num_ranks=}"
        num_half_ranks = num_ranks // 2
        half_rank = min(rank, num_ranks - 1 - rank)
        half_num_chunks = num_chunks // 2
        self.num_half_ranks = num_half_ranks
        self.half_rank = half_rank

        if not self.forward_only and (self.is_first_rank or self.is_last_rank):
            assert criterion is not None, "Criterion must be provided in training mode on first/last rank"
        
        logger.debug("Resetting internal states")
        self._reset_states()

        logger.debug(f"inputs in step: {inputs}, {half_num_chunks}, {self.batch_dim}")

        # Scatter inputs and labels into micro-batches
        inputs = scatter(inputs, half_num_chunks, self.batch_dim)
        labels = scatter(labels, half_num_chunks, self.batch_dim)
        if self.is_first_rank:
            self.input_chunks = (inputs, [])
            self.labels = ([], labels)
        elif self.is_last_rank:
            self.input_chunks = ([], inputs)
            self.labels = (labels, [])
        self.criterion = criterion
        logger.info(f"Inputs and labels scattered into {half_num_chunks} chunks per half.")

        # Step 1: nF0
        step_1 = (num_half_ranks - half_rank - 1) * 2
        logger.info(f"Step 1: Executing nF0 for {step_1} iterations.")
        for i in range(step_1):
            logger.debug(f"Step 1, iteration {i+1}/{step_1}")
            self._forward_chunk(0)

        # Step 2: nF0F1
        # In the step method, add these detailed logs for Step 2:
        # Step 2: nF0F1
        step_2 = half_rank + 1
        logger.info(f"Rank {self.rank} - Step 2: Executing nF0F1 for {step_2} iterations.")
        logger.info(f"Rank {self.rank} - Step 2: Starting self._recv_forward(0) at {time.time()}")
        self._recv_forward(0)
        logger.info(f"Rank {self.rank} - Step 2: Completed self._recv_forward(0) at {time.time()}")

        for i in range(step_2):
            start_time = time.time()
            logger.info(f"Rank {self.rank} - Step 2, iteration {i+1}/{step_2} starting at {start_time}")
            
            logger.info(f"Rank {self.rank} - Step 2: Starting self._forward_chunk(0, recv=False, send={self.is_middle_rank}) at {time.time()}")
            self._forward_chunk(0, recv=False, send=self.is_middle_rank)
            logger.info(f"Rank {self.rank} - Step 2: Completed self._forward_chunk(0) at {time.time()}")
            
            logger.info(f"Rank {self.rank} - Step 2: Starting self._recv_forward(0) at {time.time()}")
            self._recv_forward(0)
            logger.info(f"Rank {self.rank} - Step 2: Completed self._recv_forward(0) at {time.time()}")
            
            logger.info(f"Rank {self.rank} - Step 2: Starting self._forward_chunk(1, send={(not self.is_middle_rank) or (i < step_2 - 1)}) at {time.time()}")
            self._forward_chunk(1, send=(not self.is_middle_rank) or (i < step_2 - 1))
            logger.info(f"Rank {self.rank} - Step 2: Completed self._forward_chunk(1) at {time.time()}")
            
            if not self.is_middle_rank:
                logger.info(f"Rank {self.rank} - Step 2: Starting self._send_forward(0) at {time.time()}")
                self._send_forward(0)
                logger.info(f"Rank {self.rank} - Step 2: Completed self._send_forward(0) at {time.time()}")
            
            logger.info(f"Rank {self.rank} - Step 2, iteration {i+1}/{step_2} completed at {time.time()}, took {time.time()-start_time:.3f}s")

        # Step 3: nB1W1F1 (Use zero bubble)
        step_3 = num_half_ranks - half_rank - 1
        logger.info(f"Step 3: Executing nB1W1F1 for {step_3} iterations (using zero bubble).")
        for i in range(step_3):
            logger.debug(f"Step 3, iteration {i+1}/{step_3}")
            self._backward_chunk(1, enable_zb=True)
            self._recv_forward(1)
            self._weight_chunk()
            self._forward_chunk(1, recv=False)

        # Step 4 (Main step): nF0B1F1B0
        step_4 = half_num_chunks - num_ranks + half_rank + 1
        logger.info(f"Step 4: Executing main step nF0B1F1B0 for {step_4} iterations.")
        for i in range(step_4):
            logger.debug(f"Step 4, iteration {i+1}/{step_4}")
            if i == 0:
                if self.is_middle_rank:
                    logger.debug("Step 4: Middle rank branch, non-overlapped chunks.")
                    self._forward_chunk(0, recv=False, send=False)
                    self._send_forward(1)
                    self._backward_chunk(1, send=False)
                    self._send_forward(0)
                    self._send_backward(1)
                else:
                    self._forward_backward_chunk(0, 1, recv0=False)
            else:
                self._forward_backward_chunk(0, 1)
            self._forward_backward_chunk(1, 0)

        # Step 5: nB1F1B0
        step_5 = num_half_ranks - half_rank - 1
        logger.info(f"Step 5: Executing nB1F1B0 for {step_5} iterations.")
        for i in range(step_5):
            logger.debug(f"Step 5, iteration {i+1}/{step_5}")
            self._backward_chunk(1)
            self._forward_backward_chunk(1, 0)

        # Step 6: nB1B0 (The second half of the chunks use zero bubble)
        step_6 = half_rank + 1
        enable_zb = False
        logger.info(f"Step 6: Executing nB1B0 for {step_6} iterations (zero bubble switch control).")
        for i in range(step_6):
            logger.debug(f"Step 6, iteration {i+1}/{step_6} (enable_zb={enable_zb})")
            if i == step_6 // 2 and half_rank % 2 == 1:
                enable_zb = True
                logger.debug("Step 6: Enabling zero bubble (condition 1).")
            self._backward_chunk(1, enable_zb=enable_zb)
            if i == step_6 // 2 and half_rank % 2 == 0:
                enable_zb = True
                logger.debug("Step 6: Enabling zero bubble (condition 2).")
            self._backward_chunk(0, enable_zb=enable_zb)

        # Step 7: nWB0 (Use zero bubble)
        step_7 = num_half_ranks - half_rank - 1
        logger.info(f"Step 7: Executing nWB0 for {step_7} iterations (weight chunking with zero bubble).")
        for i in range(step_7):
            logger.debug(f"Step 7, iteration {i+1}/{step_7}")
            self._weight_chunk()
            self._backward_chunk(0, enable_zb=True)

        # Step 8: nW
        step_8 = half_rank + 1
        logger.info(f"Step 8: Executing nW for {step_8} iterations (final weight update).")
        for i in range(step_8):
            logger.debug(f"Step 8, iteration {i+1}/{step_8}")
            self._weight_chunk()

        assert WeightGradStore.funcs_queue.empty(), "WeightGradStore queue should be empty after weight updates."

        self._commit_and_wait_comm()
        logger.info("Communication committed and tensors freed.")

        loss, outputs = None, None
        if self.is_first_rank or self.is_last_rank:
            if criterion is not None:
                loss = torch.stack(self.loss_chunks)
                logger.info(f"Loss computed with {len(self.loss_chunks)} chunks.")
            if return_outputs:
                outputs = gather(self.output_chunks[self.is_first_rank], self.batch_dim)
                if len(outputs) == 1:
                    outputs = outputs[0]
                logger.info("Outputs gathered.")
        
        self._reset_states()
        logger.info("Step complete, internal states reset.")

        return loss, outputs
