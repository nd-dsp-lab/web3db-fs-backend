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

class DeleteFolder(BaseModel):
    folder_path: str
    user_address: str
    cids: Optional[List[str]] = []
    unpin_after: Optional[bool] = False
