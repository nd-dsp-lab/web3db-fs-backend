const { ethers } = require("hardhat");
const { create } = require("ipfs-http-client");
const fs = require("fs");

async function main() {
    // Connect to local IPFS node
    const ipfs = create({ url: 'http://localhost:5001' });

    // Deploy the FileStorage contract
    const FileStorage = await ethers.getContractFactory("FileStorage");
    const fileStorage = await FileStorage.deploy();
    await fileStorage.deployed();
    console.log("FileStorage deployed to:", fileStorage.address);

    // Test file
    const testContent = "Test file for IPFS + smart contract integration";
    fs.writeFileSync("testfile.txt", testContent);

    // Upload file to IPFS
    const { cid } = await ipfs.add({
        path: "testfile.txt",
        content: testContent
    });
    console.log("File uploaded to IPFS with CID:", cid.toString());

    // Store CID in smart contract
    const tx = await fileStorage.uploadFile(cid.toString());    // calling uploadFile function in contract
    await tx.wait();
    console.log("CID stored in smart contract");

    // Get wallet address -> calling ethers.js getSigners method
    const [owner] = await ethers.getSigners();

    // Get files from smart contract
    const userFiles = await fileStorage.getUserFiles(owner.address);
    console.log("Files in smart contract:", userFiles);

    // Now, downloand the file from IPFS using the CID
    const chunks = [];
    for await (const chunk of ipfs.cat(cid)) {
        chunks.push(chunk);
    }
    const download = Buffer.concat(chunks).toString(); // Combining chunks into one string 
    console.log("Downloaded file content from IPFS:", download);

    // Test if content matches original
    if (download === testContent) {
        console.log("Success: Downloaded content matches original");
    } else {
        console.log("Error: Downloaded content does not match original");
    }

    // Clean up
    fs.unlinkSync("testfile.txt");
}

main().catch((error) => {
    console.error(error);
    process.exitCode = 1;
});