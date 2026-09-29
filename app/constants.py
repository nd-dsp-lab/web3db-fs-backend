"""Values shared across routers and helpers."""

# What getFileOwner returns for a CID nobody has registered.
ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"

# FileStorage.ReqStatus, in declaration order. The contract returns the enum
# as an integer; the API speaks the name so clients never hardcode ordinals.
REQUEST_STATUS = ("none", "pending", "approved", "denied", "cancelled")
