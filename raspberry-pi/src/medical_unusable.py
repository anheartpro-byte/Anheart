"""The error :mod:`src.medical` raises when it is imported and its file cannot be used.

Apart from that module so that the console can name it around the import that raises
it (:mod:`src.local_panel`): a shipped file that cannot be read or parsed is a
configuration problem, said once, with the exit code the service does not restart on.

See .claude/skills/anheart-strict-python/SKILL.md.
"""

from __future__ import annotations


class MedicalParametersUnusableError(ValueError):
    """The shipped file cannot be read or parsed: the installation is broken.

    Raised once, when :mod:`src.medical` is imported, because every module that uses
    a medical value needs one to exist. A file that parses but is not signed
    does not raise: it is the bench mode.
    """

    def __init__(self, problems: tuple[str, ...]) -> None:
        self.problems: tuple[str, ...] = problems
        super().__init__("parametres medicaux inutilisables : " + " ; ".join(problems))
