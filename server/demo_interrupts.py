"""Duplicate only the interruption notice; preserve Pipecat interaction policy."""
import asyncio
from pydantic import BaseModel
from loguru import logger
from pipecat.processors.frameworks.rtvi.processor import RTVIProcessor as _RTVIProcessor

class InterruptNotice(BaseModel):
    label: str = 'rtvi-ai'
    type: str = 'bot-interrupted'
    data: dict[str, int]

# Keep the recorder's RTVIProcessor source taxonomy while using the public
# PipelineWorker rtvi_processor injection and push_transport_message hook.
class RTVIProcessor(_RTVIProcessor):
    def __init__(self, *, event=lambda *a, **kw: None, **kwargs):
        super().__init__(**kwargs)
        self._interrupt_seq=0
        self._interrupt_repeats=set()
        self._interrupt_event=event

    async def push_transport_message(self, model, exclude_none=True):
        if model.type != 'bot-interrupted':
            return await super().push_transport_message(model,exclude_none)
        self._interrupt_seq+=1
        seq=self._interrupt_seq
        notice=InterruptNotice(data={'interrupt_seq':seq})
        await super().push_transport_message(notice,exclude_none)
        self._interrupt_event('interrupt-copy',seq=seq,copy=1)
        if len(self._interrupt_repeats)>=8:
            logger.warning('Interrupt repeat dropped: bounded repeat queue full')
            self._interrupt_event('interrupt-repeat-dropped', seq=seq, capacity=8)
            return
        task=asyncio.create_task(self._repeat_interrupt(notice,seq))
        self._interrupt_repeats.add(task)
        task.add_done_callback(self._repeat_done)

    def _repeat_done(self, task):
        self._interrupt_repeats.discard(task)
        if not task.cancelled() and task.exception() is not None:
            logger.error('Interrupt repeat failed: {}', type(task.exception()).__name__)
            self._interrupt_event('interrupt-copy-error', error=type(task.exception()).__name__)

    async def _repeat_interrupt(self, notice, seq):
        await asyncio.sleep(.1)
        await super().push_transport_message(notice)
        self._interrupt_event('interrupt-copy',seq=seq,copy=2)

    async def stop_interrupt_repeats(self):
        pending=list(self._interrupt_repeats)
        for task in pending:task.cancel()
        await asyncio.gather(*pending,return_exceptions=True)
