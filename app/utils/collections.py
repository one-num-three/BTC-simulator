from __future__ import annotations

from collections import OrderedDict
from collections.abc import Iterator


class BoundedSet:
    """A set that forgets its oldest members once it reaches ``maxlen``.

    Used for gossip de-duplication. An unbounded set works fine for a one-hour
    class and then grows without limit on a node that is left running, because
    every transaction and block the node ever relayed stays in memory forever.
    """

    def __init__(self, maxlen: int = 20000):
        if int(maxlen) < 1:
            raise ValueError("maxlen must be >= 1")
        self.maxlen = int(maxlen)
        self._items: OrderedDict[str, None] = OrderedDict()

    def add(self, item: str) -> None:
        if item in self._items:
            self._items.move_to_end(item)
            return
        self._items[item] = None
        while len(self._items) > self.maxlen:
            self._items.popitem(last=False)

    def discard(self, item: str) -> None:
        self._items.pop(item, None)

    def clear(self) -> None:
        self._items.clear()

    def __contains__(self, item: object) -> bool:
        return item in self._items

    def __len__(self) -> int:
        return len(self._items)

    def __iter__(self) -> Iterator[str]:
        return iter(self._items)
