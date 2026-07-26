from pydantic import BaseModel
from typing import Optional, List

# Adding models for different file actions
class TransactionRequest(BaseModel):
    tx_hash: str

class ShareRequest(BaseModel):
    cid: str
    to_address: str
    user_address: str

class UnshareRequest(BaseModel):
    cid: str
    to_address: str
    user_address: str

class DeleteRequest(BaseModel):
    cid: str
    user_address: str
    unpin_after: Optional[bool] = False

class MoveRequest(BaseModel):
    cid: str
    new_path: str
    user_address: str

class FundWalletRequest(BaseModel):
    address: str

class ResolveRecipientRequest(BaseModel):
    recipient: str

class NotifyShareRequest(BaseModel):
    recipient_email: str
    filename: str
    sharer: str

class DeleteFolder(BaseModel):
    folder_path: str
    user_address: str
    cids: Optional[List[str]] = []
    unpin_after: Optional[bool] = False

class DeleteBatchRequest(BaseModel):
    cids: List[str]
    user_address: str

# Read-only batch lookups: the caller is identified by their auth token, so
# there is no address to supply. Callers that still send one are tolerated —
# pydantic ignores unknown fields — which keeps older clients working.
class CidBatchRequest(BaseModel):
    cids: List[str]

class MoveBatchRequest(BaseModel):
    cids: List[str]
    new_paths: List[str]
    user_address: str

class ShareBatchRequest(BaseModel):
    cids: List[str]
    to_address: str
    user_address: str

class AuthTokenRequest(BaseModel):
    address: str
    timestamp: int
    signature: str
