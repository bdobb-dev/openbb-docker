"""UUIDv7-style internal IDs (baseline §3.1, §20). String order == creation order."""
import os
import time

PREFIXES = {"iss", "ins", "lst", "ven", "idx", "cap", "run", "tkv", "evt", "mem", "wrk"}


def new_id(prefix: str) -> str:
    """Generate a new UUIDv7-style ID with the given prefix.
    
    Args:
        prefix: One of the valid prefixes (iss, ins, lst, ven, idx, cap, run, tkv, evt, mem, wrk)
        
    Returns:
        A string in the format "{prefix}_{uuid7hex}" where uuid7hex is 32 hex digits.
        Time-ordered: string order matches creation time order.
        
    Raises:
        ValueError: If prefix is not in PREFIXES.
    """
    if prefix not in PREFIXES:
        raise ValueError(f"unknown id prefix: {prefix!r}")
    
    # Get current time in milliseconds
    ms = time.time_ns() // 1_000_000
    
    # Get 10 random bytes
    rand = os.urandom(10)
    
    # Construct UUIDv7 bytes:
    # - 6 bytes of timestamp (ms)
    # - 1 byte: version (0x7) and variant bits (4 random bits)
    # - 1 byte: variant bits (10) and 6 random bits
    # - 10 bytes: random data
    b = (ms.to_bytes(6, "big") + 
         bytes([0x70 | (rand[0] & 0x0F), rand[1], 0x80 | (rand[2] & 0x3F)]) + 
         rand[3:10])
    
    return f"{prefix}_{b.hex()}"
