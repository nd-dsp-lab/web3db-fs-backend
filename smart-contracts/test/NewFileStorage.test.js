const { expect } = require("chai");
const { ethers } = require("hardhat");

describe("FileStorage", function () {
  let fileStorage;
  let owner;
  let user1;
  let user2;
  
  // Permission constants matching the contract
  const READ = 1 << 0;           // 0x01
  const WRITE = 1 << 1;          // 0x02
  const DOWNLOAD = 1 << 2;       // 0x04
  const DELETE = 1 << 3;         // 0x08
  const SHARE = 1 << 4;          // 0x10
  const MOVE = 1 << 5;           // 0x20
  const CHANGE_OWNER = 1 << 6;   // 0x40
  const CHANGE_ROLE = 1 << 7;    // 0x80
  
  beforeEach(async function () {
    // Deploy fresh contract before each test
    const FileStorage = await ethers.getContractFactory("FileStorage");
    fileStorage = await FileStorage.deploy();
    
    // Get test signers
    [owner, user1, user2] = await ethers.getSigners();
  });

  describe("File Upload", function () {
    it("Should upload and retrieve a file with metadata", async function () {
      const testCID = "QmTest123";
      const filename = "/main/folder/test.txt";
      const fileFormat = "text/plain";
      
      await fileStorage.uploadFile(testCID, filename, fileFormat);
      
      // Test retrieving files
      const files = await fileStorage.getUserFiles(owner.address);
      
      expect(files.length).to.equal(1);
      expect(files[0].cid).to.equal(testCID);
      expect(files[0].filename).to.equal(filename);
      expect(files[0].fileFormat).to.equal(fileFormat);
      // timestamp is a BigNumber -> convert to number before numeric comparison
      expect(Number(files[0].timestamp)).to.be.gt(0);
    });

    it("Should set owner as file owner on upload", async function () {
      const testCID = "QmTest456";
      
      await fileStorage.uploadFile(testCID, "test.pdf", "application/pdf");
      
      const fileOwnerAddr = await fileStorage.getFileOwner(testCID);
      expect(fileOwnerAddr).to.equal(owner.address);
    });

    it("Should give owner all permissions on upload", async function () {
      const testCID = "QmTest789";
      
      await fileStorage.uploadFile(testCID, "test.doc", "application/doc");
      
      const permissions = await fileStorage.getPermissions(testCID, owner.address);
      expect(Number(permissions)).to.equal(0xFF); // All 8 permission bits
    });

    it("Should prevent duplicate file uploads", async function () {
      const testCID = "QmDuplicate";
      
      await fileStorage.uploadFile(testCID, "file1.txt", "text/plain");
      
      await expect(
        fileStorage.uploadFile(testCID, "file2.txt", "text/plain")
      ).to.be.revertedWith("File already exists");
    });
  });

  describe("setPermissions", function () {
    let testCID;

    beforeEach(async function () {
      testCID = "QmTestPermissions";
      await fileStorage.uploadFile(testCID, "test.txt", "text/plain");
    });

    it("Should set permissions for a user", async function () {
      const permissions = READ | DOWNLOAD; // 0x05
      
      await fileStorage.setPermissions(testCID, user1.address, permissions);
      
      const userPerms = await fileStorage.getPermissions(testCID, user1.address);
      expect(Number(userPerms)).to.equal(permissions);
    });

    it("Should allow setting multiple permission combinations", async function () {
      // Test READ + WRITE + DOWNLOAD
      await fileStorage.setPermissions(testCID, user1.address, READ | WRITE | DOWNLOAD);
      expect(Number(await fileStorage.getPermissions(testCID, user1.address))).to.equal(READ | WRITE | DOWNLOAD); 
      // Test SHARE + MOVE
      await fileStorage.setPermissions(testCID, user2.address, SHARE | MOVE);
      expect(Number(await fileStorage.getPermissions(testCID, user2.address))).to.equal(SHARE | MOVE);
    });

    it("Should overwrite previous permissions", async function () {
      await fileStorage.setPermissions(testCID, user1.address, READ);
      expect(Number(await fileStorage.getPermissions(testCID, user1.address))).to.equal(READ);
      
      // Overwrite with new permissions
      await fileStorage.setPermissions(testCID, user1.address, WRITE | DELETE);
      expect(Number(await fileStorage.getPermissions(testCID, user1.address))).to.equal(WRITE | DELETE);
    });

    it("Should only allow file owner to set permissions", async function () {
      await expect(
        fileStorage.connect(user1).setPermissions(testCID, user2.address, READ)
      ).to.be.revertedWith("Not file owner");
    });

    it("Should reject invalid user address", async function () {
      await expect(
        fileStorage.setPermissions(testCID, ethers.ZeroAddress, READ)
      ).to.be.revertedWith("Invalid user");
    });

    it("Should emit PermissionGranted event", async function () {
      const permissions = READ | WRITE;
      
      await expect(fileStorage.setPermissions(testCID, user1.address, permissions))
        .to.emit(fileStorage, "PermissionGranted")
        .withArgs(testCID, user1.address, permissions);
    });
  });

  describe("grant", function () {
    let testCID;

    beforeEach(async function () {
      testCID = "QmTestGrant";
      await fileStorage.uploadFile(testCID, "test.txt", "text/plain");
    });

    it("Should grant additional permissions to a user", async function () {
      // First set READ permission
      await fileStorage.setPermissions(testCID, user1.address, READ);
      
      // Grant DOWNLOAD permission
      await fileStorage.grant(testCID, user1.address, DOWNLOAD);
      
      const permissions = await fileStorage.getPermissions(testCID, user1.address);
      expect(Number(permissions)).to.equal(READ | DOWNLOAD);
    });

    it("Should add user to sharedFiles on first grant", async function () {
      await fileStorage.grant(testCID, user1.address, READ);
      
      const files = await fileStorage.getUserFiles(user1.address);
      expect(files.length).to.equal(1);
      expect(files[0].cid).to.equal(testCID);
    });

    it("Should not duplicate in sharedFiles on subsequent grants", async function () {
      await fileStorage.grant(testCID, user1.address, READ);
      await fileStorage.grant(testCID, user1.address, WRITE);
      await fileStorage.grant(testCID, user1.address, DOWNLOAD);
      
      const files = await fileStorage.getUserFiles(user1.address);
      expect(files.length).to.equal(1); // Should still be 1, not 3
    });

    it("Should accumulate permissions with multiple grants", async function () {
      await fileStorage.grant(testCID, user1.address, READ);
      await fileStorage.grant(testCID, user1.address, WRITE);
      await fileStorage.grant(testCID, user1.address, DOWNLOAD);
      
      const permissions = await fileStorage.getPermissions(testCID, user1.address);
      expect(Number(permissions)).to.equal(READ | WRITE | DOWNLOAD);
    });

    it("Should only allow file owner to grant permissions", async function () {
      await expect(
        fileStorage.connect(user1).grant(testCID, user2.address, READ)
      ).to.be.revertedWith("Not file owner");
    });

    it("Should emit PermissionGranted event", async function () {
      await expect(fileStorage.grant(testCID, user1.address, READ | WRITE))
        .to.emit(fileStorage, "PermissionGranted")
        .withArgs(testCID, user1.address, READ | WRITE);
    });

    it("Should allow granting same permission multiple times (idempotent)", async function () {
      await fileStorage.grant(testCID, user1.address, READ);
      await fileStorage.grant(testCID, user1.address, READ);
      
      const permissions = await fileStorage.getPermissions(testCID, user1.address);
      expect(Number(permissions)).to.equal(READ);
    });
  });

  describe("revoke", function () {
    let testCID;

    beforeEach(async function () {
      testCID = "QmTestRevoke";
      await fileStorage.uploadFile(testCID, "test.txt", "text/plain");
      // Give user1 all permissions
      await fileStorage.setPermissions(testCID, user1.address, 0xFF);
    });

    it("Should revoke specific permissions", async function () {
      await fileStorage.revoke(testCID, user1.address, READ | WRITE);
      
      const permissions = await fileStorage.getPermissions(testCID, user1.address);
      expect(Number(permissions)).to.equal(0xFF & ~(READ | WRITE));
    });

    it("Should only allow file owner to revoke permissions", async function () {
      await expect(
        fileStorage.connect(user1).revoke(testCID, user2.address, READ)
      ).to.be.revertedWith("Not file owner");
    });

    it("Should emit PermissionRevoked event", async function () {
      await expect(fileStorage.revoke(testCID, user1.address, DELETE))
        .to.emit(fileStorage, "PermissionRevoked")
        .withArgs(testCID, user1.address, DELETE);
    });
  });

  describe("Permission Helper Functions", function () {
    let testCID;

    beforeEach(async function () {
      testCID = "QmTestHelpers";
      await fileStorage.uploadFile(testCID, "test.txt", "text/plain");
    });

    it("Should correctly check canRead", async function () {
      await fileStorage.grant(testCID, user1.address, READ);
      expect(await fileStorage.canRead(testCID, user1.address)).to.be.true;
      expect(await fileStorage.canRead(testCID, user2.address)).to.be.false;
    });

    it("Should correctly check canWrite", async function () {
      await fileStorage.grant(testCID, user1.address, WRITE);
      expect(await fileStorage.canWrite(testCID, user1.address)).to.be.true;
      expect(await fileStorage.canWrite(testCID, user2.address)).to.be.false;
    });

    it("Should correctly check canDownload", async function () {
      await fileStorage.grant(testCID, user1.address, DOWNLOAD);
      expect(await fileStorage.canDownload(testCID, user1.address)).to.be.true;
    });

    it("Should correctly check canShare", async function () {
      await fileStorage.grant(testCID, user1.address, SHARE);
      expect(await fileStorage.canShare(testCID, user1.address)).to.be.true;
    });

    it("Should correctly check multiple permissions", async function () {
      await fileStorage.grant(testCID, user1.address, READ | WRITE | DOWNLOAD);
      
      expect(await fileStorage.canRead(testCID, user1.address)).to.be.true;
      expect(await fileStorage.canWrite(testCID, user1.address)).to.be.true;
      expect(await fileStorage.canDownload(testCID, user1.address)).to.be.true;
      expect(await fileStorage.canDelete(testCID, user1.address)).to.be.false;
    });
  });

  describe("hasAll and hasAny", function () {
    let testCID;

    beforeEach(async function () {
      testCID = "QmTestHas";
      await fileStorage.uploadFile(testCID, "test.txt", "text/plain");
      await fileStorage.grant(testCID, user1.address, READ | WRITE | DOWNLOAD);
    });

    it("Should correctly check hasAll for exact match", async function () {
      expect(await fileStorage.hasAll(testCID, user1.address, READ | WRITE)).to.be.true;
      expect(await fileStorage.hasAll(testCID, user1.address, READ | WRITE | DOWNLOAD)).to.be.true;
    });

    it("Should return false for hasAll if missing any permission", async function () {
      expect(await fileStorage.hasAll(testCID, user1.address, READ | DELETE)).to.be.false;
    });

    it("Should correctly check hasAny for any permission", async function () {
      expect(await fileStorage.hasAny(testCID, user1.address, READ)).to.be.true;
      expect(await fileStorage.hasAny(testCID, user1.address, DELETE | READ)).to.be.true;
    });

    it("Should return false for hasAny if no permissions match", async function () {
      expect(await fileStorage.hasAny(testCID, user1.address, DELETE | SHARE)).to.be.false;
    });
  });

  describe("getUserFiles", function () {
    it("Should return owned files", async function () {
      await fileStorage.uploadFile("QmOwned1", "file1.txt", "text/plain");
      await fileStorage.uploadFile("QmOwned2", "file2.pdf", "application/pdf");
      
      const files = await fileStorage.getUserFiles(owner.address);
      expect(files.length).to.equal(2);
    });

    it("Should return shared files", async function () {
      await fileStorage.uploadFile("QmShared1", "file1.txt", "text/plain");
      await fileStorage.grant("QmShared1", user1.address, READ);
      
      const files = await fileStorage.getUserFiles(user1.address);
      expect(files.length).to.equal(1);
      expect(files[0].cid).to.equal("QmShared1");
    });

    it("Should return both owned and shared files", async function () {
      // Owner creates files
      await fileStorage.uploadFile("QmFile1", "file1.txt", "text/plain");
      await fileStorage.uploadFile("QmFile2", "file2.txt", "text/plain");
      
      // Share first file with user1
      await fileStorage.grant("QmFile1", user1.address, READ);
      
      // User1 creates their own file
      await fileStorage.connect(user1).uploadFile("QmFile3", "file3.txt", "text/plain");
      
      const files = await fileStorage.getUserFiles(user1.address);
      expect(files.length).to.equal(2); // 1 owned + 1 shared
    });

    it("Should return empty array for user with no files", async function () {
      const files = await fileStorage.getUserFiles(user1.address);
      expect(files.length).to.equal(0);
    });
  });

  describe("deleteFile", function () {
    let testCID;

    beforeEach(async function () {
      testCID = "QmTestDelete";
      await fileStorage.uploadFile(testCID, "test.txt", "text/plain");
    });

    it("Should delete file and remove from owner's files", async function () {
      await fileStorage.deleteFile(testCID);
      
      const files = await fileStorage.getUserFiles(owner.address);
      expect(files.length).to.equal(0);
      
      const fileOwnerAddr = await fileStorage.getFileOwner(testCID);
      expect(fileOwnerAddr).to.equal(ethers.ZeroAddress);
    });

    it("Should remove file from all shared users", async function () {
      await fileStorage.grant(testCID, user1.address, READ);
      await fileStorage.grant(testCID, user2.address, WRITE);
      
      // Verify users have access
      expect((await fileStorage.getUserFiles(user1.address)).length).to.equal(1);
      expect((await fileStorage.getUserFiles(user2.address)).length).to.equal(1);
      
      // Delete file
      await fileStorage.deleteFile(testCID);
      
      // Verify file removed from shared users
      expect((await fileStorage.getUserFiles(user1.address)).length).to.equal(0);
      expect((await fileStorage.getUserFiles(user2.address)).length).to.equal(0);
    });

    it("Should clear all permissions on delete", async function () {
      await fileStorage.grant(testCID, user1.address, READ | WRITE);
      
      await fileStorage.deleteFile(testCID);
      
      const permissions = await fileStorage.getPermissions(testCID, user1.address);
      expect(Number(permissions)).to.equal(0);
    });

    it("Should only allow owner to delete file", async function () {
      await expect(
        fileStorage.connect(user1).deleteFile(testCID)
      ).to.be.revertedWith("Not file owner");
    });

    it("Should emit FileDeleted event", async function () {
      await expect(fileStorage.deleteFile(testCID))
        .to.emit(fileStorage, "FileDeleted")
        .withArgs(owner.address, testCID);
    });
  });

  describe("Integration Tests", function () {
    it("Should handle complete file sharing workflow", async function () {
      // Owner uploads file
      const cid = "QmIntegration";
      await fileStorage.uploadFile(cid, "/docs/report.pdf", "application/pdf");
      
      // Owner grants read permission to user1
      await fileStorage.grant(cid, user1.address, READ | DOWNLOAD);
      
      // Verify user1 can see the file
      const user1Files = await fileStorage.getUserFiles(user1.address);
      expect(user1Files.length).to.equal(1);
      expect(user1Files[0].cid).to.equal(cid);
      
      // Verify permissions
      expect(await fileStorage.canRead(cid, user1.address)).to.be.true;
      expect(await fileStorage.canDownload(cid, user1.address)).to.be.true;
      expect(await fileStorage.canWrite(cid, user1.address)).to.be.false;
    });

    it("Should handle multiple users with different permissions", async function () {
      const cid = "QmMultiUser";
      await fileStorage.uploadFile(cid, "shared.txt", "text/plain");
      
      // Grant different permissions to different users
      await fileStorage.grant(cid, user1.address, READ);
      await fileStorage.grant(cid, user2.address, READ | WRITE | DOWNLOAD);
      
      expect(await fileStorage.canRead(cid, user1.address)).to.be.true;
      expect(await fileStorage.canWrite(cid, user1.address)).to.be.false;
      
      expect(await fileStorage.canRead(cid, user2.address)).to.be.true;
      expect(await fileStorage.canWrite(cid, user2.address)).to.be.true;
      expect(await fileStorage.canDownload(cid, user2.address)).to.be.true;
    });
  });
});