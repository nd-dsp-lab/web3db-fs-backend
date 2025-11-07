const { expect } = require("chai");
const { ethers } = require("hardhat");

// for testing contracts locally
describe("FileStorage", function () {
  it("Should upload and retrieve a file", async function () {
    // Deploy the contract
    const FileStorage = await ethers.getContractFactory("FileStorage");
    const fileStorage = await FileStorage.deploy();
    
    // Test uploading a file
    const testCID = "QmTest123";    // test CID, all start with Qm
    await fileStorage.uploadFile(testCID);
    
    // Test retrieving files
    const [owner] = await ethers.getSigners();
    const files = await fileStorage.getUserFiles(owner.address);
    
    expect(files.length).to.equal(1);
    expect(files[0]).to.equal(testCID);
  });
});