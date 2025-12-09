const { ethers } = require("hardhat");

// deploy.js updated for ethers v6 compatibilitiy
async function main() {
    console.log("Deploying FileStorage Contract...");
    const Contract = await ethers.getContractFactory("FileStorage");
    const contract = await Contract.deploy();
    console.log("Contract deployed to:", contract.target);
}

main().catch((error) => {
        console.error(error);
        process.exit(1);
    });