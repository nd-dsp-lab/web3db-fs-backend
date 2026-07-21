import os
import json
import logging
from dotenv import load_dotenv
from fastapi.middleware.cors import CORSMiddleware
from web3 import Web3

from logging_config import setup_logging

# configure is the first backend module imported, so set up logging here
# before anything else emits a record.
setup_logging()
logger = logging.getLogger(__name__)

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

    logger.info("server started listening on port 8090")

# Constant
IPFS_API_URL = "http://localhost:5001/api/v0"
IPFS_GATEWAY_URL = "http://localhost:8082/ipfs"

# connect to web3 via infura --> use environment variable
infura_url = os.getenv("INFURA_URL") or f"https://sepolia.infura.io/v3/{os.getenv('INFURA_API_KEY')}"
w3 = Web3(Web3.HTTPProvider(infura_url))
try:
    if w3.is_connected():
        logger.info("Web3 is connected")
except Exception as e:
    logger.warning("Could not verify Web3 connection at startup: %s", e)
    logger.warning("Web3 will be tested when making transactions")

# load contract from Will's deployed contract
contract_path = os.path.join(os.path.dirname(__file__), 'FileStorage.json')
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
logger.info("Contract loaded: %s", contract.address)