"""Chats are separate conversations with their own settings, materials, and history.

The application opens on a home page with a list of chats, not straight on the working
screen: preparing for an exam and an interview are different conversations, and the tone,
materials, and language of one must not leak into the other. Layout:

* `store`   — the chat folder on disk;
* `manager` — which chat is open, how its settings lay over the shared ones, how history is
  written and returned;
* `routes`  — data for the home page.
"""

from argus.chats.manager import ChatManager, chats_of, manager_of

__all__ = ["ChatManager", "chats_of", "manager_of"]
