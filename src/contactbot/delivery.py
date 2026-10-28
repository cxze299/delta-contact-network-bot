"""Exceptions at the boundary before Core may have accepted a message."""


class DeliveryNotStarted(RuntimeError):
    """A failure known to precede the send RPC; it is safe to try again."""


def filename(token: str) -> str:
    # An opaque random identifier, not an account address or member identifier.
    return "contact-" + token + ".vcf"
