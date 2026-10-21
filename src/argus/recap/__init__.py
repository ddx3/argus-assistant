"""Conversation recap and watch topics.

The user asks the assistant to sum up the conversation («итог» (recap), or a button on
the page) or to watch a topic («следи за сроками» (watch the deadlines)): at the end
there will be a recap on it as well. The structure:

* `lines`    — utterances of the conversation: from the chat log, cut into parts by size;
* `prompts`  — texts of requests to the model;
* `engine`   — transcript → requests → recap text (long ones in parts);
* `commands` — which words in the question line count as commands;
* `topics`   — watch topics, one set per chat;
* `chat`     — the only place that knows about chats;
* `service`  — request → card in the timeline;
* `routes`   — for the page: topics and the button.

It is built into the rest of the code in two places: `Argus.ask` (`app.py`) asks
`intercept` before sending the line to the model, and `ui/server.py` hooks up
`routes.attach`.
"""

from argus.recap.service import intercept, recap_of

__all__ = ["intercept", "recap_of"]
