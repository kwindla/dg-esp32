"""DUO_CONTEXT=b2h: who answers is a marker on the user's message.

Context design B2h (impl/CTX-EVAL.md). The session has one system prompt
that describes both characters. Each stored user message ends with
`` [ANGEL]`` or `` [DEVIL]``, naming the character who answers it. Replies are
stored as spoken, with no label. Nothing else tells the model who speaks:
there is no prompt swap and no note in the conversation.

The marker is written in three places, and only there.

1. ``TaggedContext.add_message`` tags a user message as it is stored, for the
   character of that moment. So no stored user message is without a marker,
   also one whose turn never reached the LLM.

2. ``TaggedLLM.process_frame`` sets the marker of the newest user message to
   the committed character as the request leaves for the model, in the same
   synchronous step, if that message is still pending. This is how a switch
   reaches the context. A tap or an auto-switch commits the character while
   the turn gate is closed; the turn that was held, or the turn that is run
   again, passes here afterwards and gets the new character's marker. A
   switch that is rolled back never committed, so the turn run again keeps
   the old one. For a turn that no switch touched this step changes nothing.

3. A committed auto-switch marks the turn it was claimed for
   (``SwitchCoordinator._route_turn``), if still pending, so that turn is the
   new character's even if a later turn takes its place before it is run
   again.

A user message is pending until a reply to it is stored, whole or cut off.
From then on its marker says who answered it and is never written again.
Which message a reply answers is not read from the order of the history (an
interrupted reply can be stored after the next user message). The LLM names
the message on the reply's start frame, ``SpeakingCharacter`` reads it in
output order, and the context notes it when it stores the reply. A draft that
is withheld never gets that far, so its turn stays pending for the rerun.

So a stored message changes only between being stored and being answered:

- a turn held through a switch: it had not been sent;
- the turn an auto-switch was claimed for: it had been sent once, for the
  draft that was withheld.

Every answered message is the same bytes in every later request, which only
adds to the one before it.

Two things keep that true when no new words arrive.

- An empty user turn that interrupted a reply. Pipecat answers it by adding a
  developer instruction to the conversation and running the LLM. With b2h
  the aggregator is configured (``EmptyUserTurnConfig``) to add
  ``duo_prompts.INAUDIBLE`` instead, and ``TaggedContext`` stores that as a
  user message with the current character's marker: ``(inaudible) [DEVIL]``.
  The request then ends in a pending user message like any other, nothing
  old is touched, and no instruction enters the conversation. The prompt's
  own rule for a message too broken to guess makes the character ask again.
  Such a turn has no words that could address anyone, so it is not routed:
  the committed character answers it (``SwitchCoordinator._unrouted``). A
  user who really says "inaudible" is an ordinary turn and is routed.
- Any other request whose newest user message is already answered is sent as
  it is while that marker still names the committed character. If the
  character has changed since, the request is withdrawn (``DUO
  request-withdrawn``): the old marker must not be rewritten, and the new
  voice must not speak a reply written under it. Nothing in this bot makes
  such a request; it is a guard. A request like this is not routed either.

The model's own text is cleaned where it leaves the LLM
(``TaggedLLM.push_frame``): a marker or an old-style speaker label it wrote is
removed there, before the voice, the device and the history see the reply.
A reply that had one taken out is logged as ``DUO marker-removed`` with the
count, so a live run can tell how often the model writes them.
"""

import json
import re

from loguru import logger
from pipecat.frames.frames import (
    InterruptionFrame, LLMContextFrame, LLMFullResponseEndFrame, LLMFullResponseStartFrame, LLMTextFrame,
)
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.frame_processor import FrameDirection

from duo_addressee import _text
from duo_prompts import INAUDIBLE, history_line, tagged, untagged

DOWN = FrameDirection.DOWNSTREAM
# A marker, or a speaker label of the labelled design, that the model wrote
# itself. The B2h prompt forbids both and the evaluation saw neither in 492
# replies.
MARK = re.compile(r"\[(?:angel|devil)\]\s*", re.IGNORECASE)
MARKS = ("[angel]", "[devil]")


class Unmarked:
    """A reply's text without markers, as it streams: one can arrive split over several pieces."""
    def __init__(self):
        self.kept = ""       # An ending that may still turn into a marker.
        self.after = False   # The text so far ended in a marker: whitespace that follows belongs to it.
        self.removed = 0     # Markers and labels taken out of this reply.

    def feed(self, text):
        text = self.kept + text
        self.kept = ""
        if self.after:
            text = text.lstrip()
            if not text:
                return ""
        clean, end = [], 0
        for mark in MARK.finditer(text):
            clean.append(text[end:mark.start()])
            end = mark.end()
            self.removed += 1
        self.after = end > 0 and end == len(text)
        text = "".join(clean) + text[end:]
        cut = text.rfind("[")
        # Keep back an ending that may still turn into a marker; it is judged with the text that follows.
        if cut >= 0 and any(mark.startswith(text[cut:].lower()) for mark in MARKS):
            self.kept, text = text[cut:], text[:cut]
        return text

    def finish(self):
        """What was kept back and did not become a marker."""
        kept, self.kept, self.after = self.kept, "", False
        return kept


def newest_user(messages):
    """The newest user message with text, or None."""
    user = next((m for m in reversed(messages) if m.get("role") == "user"), None)
    return user if user is not None and isinstance(user.get("content"), str) else None


def stamp(messages, character):
    """Give the newest user message this character's marker. True if that changed it.

    For a pending message only; the callers ask ``TaggedContext.answered`` first.
    """
    user = newest_user(messages)
    if user is None:
        return False
    wanted = tagged(user["content"], character)
    if user["content"] == wanted:
        return False
    user["content"] = wanted  # In place: the route and the timing rows know this turn by this object.
    return True


class TaggedContext(LLMContext):
    """The stored conversation: tagged user messages, replies as spoken.

    ``speaking_character`` is set in output order by ``SpeakingCharacter``,
    before the assistant aggregator stores the reply it belongs to. The
    character is kept beside the stored reply, because nothing in the reply
    says who spoke it and the Jev read needs to know.
    """
    speaking_character = "angel"
    replying_to = None  # The user message the reply now at the output answers; SpeakingCharacter sets it.

    def __init__(self):
        super().__init__()
        self.state = None  # The switch state, once the coordinator exists.
        self.spoken = []   # (stored reply, the character who spoke it)
        self._answered = set()  # id() of user messages with a stored reply. The context keeps them alive.
        self._unheard = set()   # id() of the user messages stored for an empty turn, not for words.

    def answered(self, message):
        """Whether a reply to this user message is stored, whole or cut off. Its marker is then final."""
        return id(message) in self._answered

    def unheard(self, message):
        """Whether this user message stands for an empty turn. Known by where it came from, not by its text."""
        return id(message) in self._unheard

    def add_message(self, message):
        unheard = message.get("role") == "developer" and message.get("content") == INAUDIBLE
        if unheard:
            # Pipecat's recovery of an empty turn that interrupted a reply (build_worker configures
            # it to write this). Here it is the human's turn, heard and not understood.
            message = dict(role="user", content=INAUDIBLE)
        if message.get("role") == "user" and isinstance(message.get("content"), str):
            current = self.state.character if self.state else self.speaking_character
            message = dict(message, content=tagged(message["content"], current))
            if unheard:
                self._unheard.add(id(message))
        elif message.get("role") == "assistant" and isinstance(message.get("content"), str):
            said = MARK.sub("", message["content"])
            if not said.strip():
                return  # Markers only: nothing was said.
            message = dict(message, content=said)
            self.spoken.append((message, self.speaking_character))
            if self.replying_to is not None:
                self._answered.add(id(self.replying_to))
        super().add_message(message)

    def speaker(self, message):
        return next((character for reply, character in self.spoken if reply is message), None)

    def labelled_history(self, messages):
        """The conversation as ``addressee_state`` reads it, so Jev is asked what it is asked with labels.

        Returns what ``split_context`` returns for a labelled conversation:
        the messages before the newest user message, each reply under its
        speaker's label, and that user message's words. No marker in either.
        """
        index = next((i for i in range(len(messages) - 1, -1, -1) if messages[i].get("role") == "user"), None)
        if index is None:
            return [], ""
        prior = []
        for message in messages[:index]:
            said = _text(message.get("content"))
            if message.get("role") == "user":
                prior.append(dict(role="user", content=untagged(said)))
            elif message.get("role") == "assistant":
                speaker = self.speaker(message)
                if speaker:
                    prior.append(dict(role="assistant", content=history_line(speaker, said)))
        return prior, untagged(_text(messages[index].get("content")))


class TaggedLLM:
    """Mixed into the session's LLM service: the marker in, no marker out."""
    duo_state = None    # The switch state; build_worker sets it.
    duo_context = None  # The stored conversation (TaggedContext); build_worker sets it.

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._unmarked = Unmarked()
        self._text_metadata = {}
        self._request_user = None  # The user message the request in progress answers.

    def _report_removed(self):
        if self._unmarked.removed:
            logger.info("DUO marker-removed {}", json.dumps(dict(count=self._unmarked.removed)))
            self._unmarked.removed = 0

    async def queue_frame(self, frame, direction=DOWN, callback=None):
        if isinstance(frame, InterruptionFrame):
            # At arrival: the End frame the cancelled request still pushes must not release kept text.
            self._report_removed()
            self._unmarked = Unmarked()
        await super().queue_frame(frame, direction, callback)

    async def process_frame(self, frame, direction):
        if isinstance(frame, LLMContextFrame) and self.duo_state is not None:
            messages, character = frame.context.get_messages(), self.duo_state.character
            user = newest_user(messages)
            if user is None or not self.duo_context.answered(user):
                stamp(messages, character)
            elif user["content"] != tagged(user["content"], character):
                # No new words, and the character changed since this message was answered. Its
                # marker is history, and the new voice must not speak a reply written under it.
                logger.info("DUO request-withdrawn {}", json.dumps(dict(reason="answered-turn", character=character)))
                return
            self._request_user = user
        await super().process_frame(frame, direction)

    async def push_frame(self, frame, direction=DOWN):
        if direction == DOWN:
            if isinstance(frame, LLMFullResponseStartFrame):
                self._unmarked, self._text_metadata = Unmarked(), {}
                frame.metadata["duo-user"] = self._request_user  # Read where the reply is stored.
            elif isinstance(frame, LLMTextFrame):
                self._text_metadata = dict(frame.metadata)
                frame.text = self._unmarked.feed(frame.text)
                if not frame.text:
                    return
            elif isinstance(frame, LLMFullResponseEndFrame):
                self._report_removed()
                rest = self._unmarked.finish()
                if rest:
                    # Not a marker after all. It goes out as the reply's last text, owned as that text was.
                    tail = LLMTextFrame(rest)
                    tail.metadata.update(self._text_metadata)
                    await super().push_frame(tail, direction)
        await super().push_frame(frame, direction)
