const dotenv = require("dotenv");
require("@nomiclabs/hardhat-ethers");

dotenv.config()

console.log("Environment variables loaded:");
console.log("INFURA_API_KEY:", process.env.INFURA_API_KEY ? "Present" : "Missing");
console.log("PRIVATE_KEY:", process.env.PRIVATE_KEY ? "Present" : "Missing");
console.log("ETHERSCAN_API_KEY:", process.env.ETHERSCAN_API_KEY ? "Present" : "Missing");

module.exports = {
  solidity: "0.8.28",
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