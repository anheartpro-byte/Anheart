"""Hand-written types for pyobjc's ``Foundation`` (pyobjc-framework-Cocoa 12.2.x).

Only the members ``src/bitalino_rfcomm_macos.py`` touches, which is the one
module allowed to import pyobjc (contract rule 5). No module-level
``__getattr__``: an unstubbed member must fail the build.
"""

from typing import Self

class NSObject:
    @classmethod
    def alloc(cls) -> Self: ...
    def init(self) -> Self: ...

class NSDate(NSObject):
    @classmethod
    def dateWithTimeIntervalSinceNow_(cls, seconds: float, /) -> NSDate: ...

class NSRunLoop(NSObject):
    @classmethod
    def currentRunLoop(cls) -> NSRunLoop: ...
    def runUntilDate_(self, limit: NSDate, /) -> None: ...
