import os
import json
from dotenv import load_dotenv
from fastapi.middleware.cors import CORSMiddleware
from web3 import Web3

# Load environment variables from .env file in web3db-fs-backend folder
env_path = os.path.join(os.path.dirname(__file__), '..', '.env')
load_dotenv(dotenv_path=env_path)

# Allow frontend origin
origins = [
    "http://localhost:3000",
    "http://fs.web3db.org",
    "https://fs.web3db.org",
    "https://proxy.web3db.org",
]

def configure_app(app):
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],  #or ["*"] for all origins
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    print(f"server started listening on port 8090")

# Constant
IPFS_API_URL = "http://localhost:5001/api/v0"

# connect to web3 via infura --> use environment variable
infura_url = os.getenv("INFURA_URL") or f"https://sepolia.infura.io/v3/{os.getenv('INFURA_API_KEY')}"
w3 = Web3(Web3.HTTPProvider(infura_url))
try:
    if w3.is_connected():
        print("Web3 is connected:", True)
except Exception as e:
    print(f"Warning: Could not verify Web3 connection at startup: {e}")
    print("Web3 will be tested when making transactions")

# load contract from Will's deployed contract
contract_path = os.path.join(os.path.dirname(__file__), '..', 'smart-contracts', 'artifacts', 'contracts', 'FileStorage.sol', 'FileStorage.json')
with open(contract_path) as f:
    abi = json.load(f)["abi"]
if not abi:
    raise Exception("ABI not found")

contract_address = os.getenv("CONTRACT_ADDRESS")
if not contract_address:
    raise Exception("CONTRACT_ADDRESS environment variable not found")
contract = w3.eth.contract(address=contract_address, abi=abi)
if not contract:
    raise Exception("Contract not found")
print("Contract loaded:", contract.address)