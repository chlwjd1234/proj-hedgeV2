"""Failures preserve the difference between absent evidence and contradictions."""
class ResearchError(Exception):
    pass
class Unavailable(ResearchError):
    """Data, dependency, or supported semantics are unavailable; never a PASS."""
class Unsupported(Unavailable):
    pass
class ValidationError(ResearchError):
    """Available evidence contradicts the required invariant."""
class InsufficientLiquidity(ResearchError):
    pass
