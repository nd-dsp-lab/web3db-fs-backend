from fastapi import FastAPI, UploadFile, Form
import requests
from fastapi.middleware.cors import CORSMiddleware

# This will be a simple fastAPI server that acts as an sgx node 
app = FastAPI()


# Allow frontend origin
origins = [
    "http://localhost:3000",
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,  # or ["*"] for all origins (less secure)
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


print(f"server started listening on port 8000")
IPFS_API_URL = "http://localhost:5001/api/v0"

# Register the file to ipfs and get a cid 
@app.post("/upload")
async def upload_file(file: UploadFile, user_address: str = Form(...)):
    # send the file to IPFS
    files = {"file": (file.filename, await file.read())}
    response = requests.post(f"{IPFS_API_URL}/add", files=files)
    cid = response.json()["Hash"]

    # return the cid and user address
    return {"user": user_address, "cid": cid}

# call smart contract
