const path = require("path");
const dotenv = require("dotenv");
require("@nomicfoundation/hardhat-toolbox");

dotenv.config({ path: path.resolve(__dirname, "../.env") });

console.log("Environment variables loaded:");
console.log("INFURA_API_KEY:", process.env.INFURA_API_KEY ? "Present" : "Missing");
console.log("PRIVATE_KEY:", process.env.PRIVATE_KEY ? "Present" : "Missing");
console.log("ETHERSCAN_API_KEY:", process.env.ETHERSCAN_API_KEY ? "Present" : "Missing");

module.exports = {
  solidity: {
    version: "0.8.28",
    // The request functions pushed the contract past the 24576-byte EIP-170
    // deploy limit, which Sepolia enforces too. runs=200 is the usual
    // trade-off: optimise for repeated calls, which is what this contract
    // does all day, while getting the size back under the cap.
    settings: { optimizer: { enabled: true, runs: 200 } },
  },
  networks: {
      // localhost:
      hardhat: {},
      sepolia: {
        url: `https://sepolia.infura.io/v3/${process.env.INFURA_API_KEY}`,
        accounts: [process.env.PRIVATE_KEY ? `0x${process.env.PRIVATE_KEY}` : undefined].filter(Boolean)
      },
    },
  etherscan: {
    apiKey: process.env.ETHERSCAN_API_KEY,
  }, 
};