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

  describe("grant", function () {
    let testCID;

    beforeEach(async function () {
      testCID = "QmTestGrant";
      await fileStorage.uploadFile(testCID, "test.txt", "text/plain");
    });

    it("Should set distinct permission combinations per user", async function () {
      await fileStorage.grant(testCID, user1.address, READ | WRITE | DOWNLOAD);
      expect(Number(await fileStorage.getPermissions(testCID, user1.address))).to.equal(READ | WRITE | DOWNLOAD);
      await fileStorage.grant(testCID, user2.address, SHARE | MOVE);
      expect(Number(await fileStorage.getPermissions(testCID, user2.address))).to.equal(SHARE | MOVE);
    });

    it("Should reject granting to the zero address", async function () {
      await expect(
        fileStorage.grant(testCID, ethers.ZeroAddress, READ)
      ).to.be.revertedWith("Invalid user");
    });

    it("Should grant additional permissions to a user", async function () {
      // Seed READ, then add DOWNLOAD
      await fileStorage.grant(testCID, user1.address, READ);

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
      await fileStorage.grant(testCID, user1.address, 0xFF);
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

    it("Should drop the file from the user's list when fully revoked", async function () {
      // user1 holds two shared files [testCID, QmRevokeB]; fully revoking the
      // first (non-last) exercises the swap-and-pop compaction.
      await fileStorage.uploadFile("QmRevokeB", "/b.txt", "txt");
      await fileStorage.grant("QmRevokeB", user1.address, READ);

      await fileStorage.revoke(testCID, user1.address, 0xFF);

      expect(Number(await fileStorage.getPermissions(testCID, user1.address))).to.equal(0);
      const files = await fileStorage.getUserFiles(user1.address);
      expect(files.map((f) => f.cid)).to.have.members(["QmRevokeB"]);
      // and the owner's shared-users list no longer includes user1 for testCID
      expect(Array.from(await fileStorage.getSharedUsers(testCID))).to.not.include(user1.address);
    });

    it("Should keep the file listed when only some permissions are revoked", async function () {
      await fileStorage.revoke(testCID, user1.address, WRITE);
      const files = await fileStorage.getUserFiles(user1.address);
      expect(files.map((f) => f.cid)).to.include(testCID);
    });

    it("Should fully revoke the last-listed shared file", async function () {
      // user1 holds [testCID, QmRevokeC]; fully revoking the second (last)
      // finds the match past index 0 and pops it without a swap.
      await fileStorage.uploadFile("QmRevokeC", "/c.txt", "txt");
      await fileStorage.grant("QmRevokeC", user1.address, READ);

      await fileStorage.revoke("QmRevokeC", user1.address, READ);

      const files = await fileStorage.getUserFiles(user1.address);
      expect(files.map((f) => f.cid)).to.have.members([testCID]);
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

    it("Should correctly check canDelete", async function () {
      await fileStorage.grant(testCID, user1.address, DELETE);
      expect(await fileStorage.canDelete(testCID, user1.address)).to.be.true;
      expect(await fileStorage.canDelete(testCID, user2.address)).to.be.false;
    });

    it("Should correctly check canMove", async function () {
      await fileStorage.grant(testCID, user1.address, MOVE);
      expect(await fileStorage.canMove(testCID, user1.address)).to.be.true;
      expect(await fileStorage.canMove(testCID, user2.address)).to.be.false;
    });

    it("Should correctly check canChangeOwner", async function () {
      await fileStorage.grant(testCID, user1.address, CHANGE_OWNER);
      expect(await fileStorage.canChangeOwner(testCID, user1.address)).to.be.true;
      expect(await fileStorage.canChangeOwner(testCID, user2.address)).to.be.false;
    });

    it("Should correctly check canChangeRole", async function () {
      await fileStorage.grant(testCID, user1.address, CHANGE_ROLE);
      expect(await fileStorage.canChangeRole(testCID, user1.address)).to.be.true;
      expect(await fileStorage.canChangeRole(testCID, user2.address)).to.be.false;
    });

    it("Should grant the owner every capability on upload", async function () {
      expect(await fileStorage.canRead(testCID, owner.address)).to.be.true;
      expect(await fileStorage.canWrite(testCID, owner.address)).to.be.true;
      expect(await fileStorage.canDownload(testCID, owner.address)).to.be.true;
      expect(await fileStorage.canDelete(testCID, owner.address)).to.be.true;
      expect(await fileStorage.canShare(testCID, owner.address)).to.be.true;
      expect(await fileStorage.canMove(testCID, owner.address)).to.be.true;
      expect(await fileStorage.canChangeOwner(testCID, owner.address)).to.be.true;
      expect(await fileStorage.canChangeRole(testCID, owner.address)).to.be.true;
    });
  });

  describe("getSharedUsers", function () {
    let testCID;

    beforeEach(async function () {
      testCID = "QmTestSharedUsers";
      await fileStorage.uploadFile(testCID, "test.txt", "text/plain");
    });

    it("Should be empty before any grant", async function () {
      expect((await fileStorage.getSharedUsers(testCID)).length).to.equal(0);
    });

    it("Should list every user a file is shared with", async function () {
      await fileStorage.grant(testCID, user1.address, READ);
      await fileStorage.grant(testCID, user2.address, READ | DOWNLOAD);

      // getSharedUsers returns an ethers Result (a read-only proxy); copy to a
      // plain array so chai's members matcher can sort it.
      const shared = Array.from(await fileStorage.getSharedUsers(testCID));
      expect(shared).to.have.members([user1.address, user2.address]);
    });

    it("Should not list a user twice across multiple grants", async function () {
      await fileStorage.grant(testCID, user1.address, READ);
      await fileStorage.grant(testCID, user1.address, DOWNLOAD);

      const shared = Array.from(await fileStorage.getSharedUsers(testCID));
      expect(shared).to.have.members([user1.address]);
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

    it("Should keep other files when deleting a non-last file", async function () {
      // testCID is already at index 0; add a second so deleting the first
      // exercises the swap-and-pop path (i != len - 1), not just the tail pop.
      await fileStorage.uploadFile("QmDeleteKeep", "/keep.txt", "text/plain");

      await fileStorage.deleteFile(testCID);

      const files = await fileStorage.getUserFiles(owner.address);
      expect(files.map((f) => f.cid)).to.have.members(["QmDeleteKeep"]);
      expect(await fileStorage.getFileOwner("QmDeleteKeep")).to.equal(owner.address);
    });

    it("Should compact a recipient's list when deleting a non-last shared file", async function () {
      // user1 holds two shared files [testCID, QmDelShareB]; deleting the
      // first exercises the swap-and-pop in the recipient's sharedFiles.
      await fileStorage.uploadFile("QmDelShareB", "/b.txt", "txt");
      await fileStorage.grant(testCID, user1.address, READ);
      await fileStorage.grant("QmDelShareB", user1.address, READ);

      await fileStorage.deleteFile(testCID);

      const files = await fileStorage.getUserFiles(user1.address);
      expect(files.map((f) => f.cid)).to.have.members(["QmDelShareB"]);
    });

    it("Should compact when deleting the last shared file in a recipient's list", async function () {
      // user1 holds [testCID, QmDelShareC]; deleting the second (last) finds
      // the match past index 0 and pops it without a swap.
      await fileStorage.uploadFile("QmDelShareC", "/c.txt", "txt");
      await fileStorage.grant(testCID, user1.address, READ);
      await fileStorage.grant("QmDelShareC", user1.address, READ);

      await fileStorage.deleteFile("QmDelShareC");

      const files = await fileStorage.getUserFiles(user1.address);
      expect(files.map((f) => f.cid)).to.have.members([testCID]);
    });
  });

  describe("uploadFiles (batch)", function () {
    it("Should register several files in one transaction", async function () {
      await fileStorage.uploadFiles(
        ["QmBatchUp1", "QmBatchUp2"],
        ["/docs/a.txt", "/docs/sub/b.txt"],
        ["txt", "txt"]
      );
      const files = await fileStorage.getUserFiles(owner.address);
      expect(files.map((f) => f.filename)).to.have.members(["/docs/a.txt", "/docs/sub/b.txt"]);
      expect(await fileStorage.getFileOwner("QmBatchUp1")).to.equal(owner.address);
      expect(await fileStorage.getPermissions("QmBatchUp2", owner.address)).to.equal(0xFF);
    });

    it("Should reject mismatched array lengths", async function () {
      await expect(fileStorage.uploadFiles(["QmA", "QmB"], ["/a.txt"], ["txt", "txt"]))
        .to.be.revertedWith("Length mismatch");
    });

    it("Should revert entirely on a duplicate cid in the batch", async function () {
      await fileStorage.uploadFile("QmDup", "/dup.txt", "txt");
      await expect(fileStorage.uploadFiles(["QmNew", "QmDup"], ["/n.txt", "/d.txt"], ["txt", "txt"]))
        .to.be.revertedWith("File already exists");
      expect(await fileStorage.getFileOwner("QmNew")).to.equal(ethers.ZeroAddress);
    });
  });

  describe("moveFiles (batch)", function () {
    beforeEach(async function () {
      await fileStorage.uploadFile("QmMove1", "/proj/a.txt", "txt");
      await fileStorage.uploadFile("QmMove2", "/proj/b.txt", "txt");
    });

    it("Should move several files in one transaction", async function () {
      await fileStorage.moveFiles(["QmMove1", "QmMove2"], ["/renamed/a.txt", "/renamed/b.txt"]);
      const files = await fileStorage.getUserFiles(owner.address);
      expect(files.map((f) => f.filename)).to.have.members(["/renamed/a.txt", "/renamed/b.txt"]);
    });

    it("Should emit FileMoved per file", async function () {
      await expect(fileStorage.moveFiles(["QmMove1"], ["/x/a.txt"]))
        .to.emit(fileStorage, "FileMoved")
        .withArgs(owner.address, "QmMove1", "/x/a.txt");
    });

    it("Should reject mismatched array lengths", async function () {
      await expect(fileStorage.moveFiles(["QmMove1", "QmMove2"], ["/only-one.txt"]))
        .to.be.revertedWith("Length mismatch");
    });

    it("Should revert entirely if caller doesn't own every file", async function () {
      await fileStorage.connect(user1).uploadFile("QmTheirs", "/theirs.txt", "txt");
      await expect(fileStorage.moveFiles(["QmMove1", "QmTheirs"], ["/a.txt", "/steal.txt"]))
        .to.be.revertedWith("Not file owner");
      // first file's move must have been rolled back too
      const files = await fileStorage.getUserFiles(owner.address);
      expect(files.map((f) => f.filename)).to.include("/proj/a.txt");
    });

    it("Should keep single moveFile working", async function () {
      await fileStorage.moveFile("QmMove1", "/single/a.txt");
      const files = await fileStorage.getUserFiles(owner.address);
      expect(files.map((f) => f.filename)).to.include("/single/a.txt");
    });
  });

  describe("grantFiles / revokeFiles (batch)", function () {
    beforeEach(async function () {
      await fileStorage.uploadFile("QmShare1", "/proj/a.txt", "txt");
      await fileStorage.uploadFile("QmShare2", "/proj/b.txt", "txt");
    });

    it("Should grant permissions on several files in one transaction", async function () {
      await fileStorage.grantFiles(["QmShare1", "QmShare2"], user1.address, READ | DOWNLOAD);
      expect(await fileStorage.getPermissions("QmShare1", user1.address)).to.equal(READ | DOWNLOAD);
      expect(await fileStorage.getPermissions("QmShare2", user1.address)).to.equal(READ | DOWNLOAD);
      const files = await fileStorage.getUserFiles(user1.address);
      expect(files.map((f) => f.filename)).to.have.members(["/proj/a.txt", "/proj/b.txt"]);
    });

    it("Should revoke permissions on several files in one transaction", async function () {
      await fileStorage.grantFiles(["QmShare1", "QmShare2"], user1.address, READ | DOWNLOAD);
      await fileStorage.revokeFiles(["QmShare1", "QmShare2"], user1.address, READ | DOWNLOAD);
      expect(await fileStorage.getPermissions("QmShare1", user1.address)).to.equal(0);
      expect(await fileStorage.getPermissions("QmShare2", user1.address)).to.equal(0);
      expect((await fileStorage.getUserFiles(user1.address)).length).to.equal(0);
      expect((await fileStorage.getSharedUsers("QmShare1")).length).to.equal(0);
    });

    it("Should revert entirely if caller doesn't own every file", async function () {
      await fileStorage.connect(user1).uploadFile("QmTheirShare", "/theirs.txt", "txt");
      await expect(
        fileStorage.grantFiles(["QmShare1", "QmTheirShare"], user2.address, READ)
      ).to.be.revertedWith("Not file owner");
      expect(await fileStorage.getPermissions("QmShare1", user2.address)).to.equal(0);
    });

    it("Should keep single grant and revoke working", async function () {
      await fileStorage.grant("QmShare1", user1.address, READ);
      expect(await fileStorage.getPermissions("QmShare1", user1.address)).to.equal(READ);
      await fileStorage.revoke("QmShare1", user1.address, READ);
      expect(await fileStorage.getPermissions("QmShare1", user1.address)).to.equal(0);
    });
  });

  describe("cleanFolder", function () {
    it("Should delete every owned file in the batch", async function () {
      await fileStorage.uploadFile("QmCf1", "/folder/a.txt", "txt");
      await fileStorage.uploadFile("QmCf2", "/folder/b.txt", "txt");
      await fileStorage.uploadFile("QmKeep", "/other/c.txt", "txt");

      await fileStorage.cleanFolder(["QmCf1", "QmCf2"]);

      expect(await fileStorage.getFileOwner("QmCf1")).to.equal(ethers.ZeroAddress);
      expect(await fileStorage.getFileOwner("QmCf2")).to.equal(ethers.ZeroAddress);
      // files outside the batch are untouched
      expect(await fileStorage.getFileOwner("QmKeep")).to.equal(owner.address);
      const files = await fileStorage.getUserFiles(owner.address);
      expect(files.map((f) => f.cid)).to.have.members(["QmKeep"]);
    });

    it("Should unshare deleted files from their recipients", async function () {
      await fileStorage.uploadFile("QmCfShared", "/folder/a.txt", "txt");
      await fileStorage.grant("QmCfShared", user1.address, READ);
      expect((await fileStorage.getUserFiles(user1.address)).length).to.equal(1);

      await fileStorage.cleanFolder(["QmCfShared"]);

      expect((await fileStorage.getUserFiles(user1.address)).length).to.equal(0);
      expect(await fileStorage.getPermissions("QmCfShared", user1.address)).to.equal(0);
    });

    it("Should skip cids the caller does not own without reverting", async function () {
      await fileStorage.uploadFile("QmMine", "/folder/a.txt", "txt");
      await fileStorage.connect(user1).uploadFile("QmTheirs", "/folder/b.txt", "txt");

      // Mixed batch: the caller owns QmMine but not QmTheirs. cleanFolder must
      // delete what it owns and leave the rest alone — never revert.
      await fileStorage.cleanFolder(["QmMine", "QmTheirs"]);

      expect(await fileStorage.getFileOwner("QmMine")).to.equal(ethers.ZeroAddress);
      expect(await fileStorage.getFileOwner("QmTheirs")).to.equal(user1.address);
    });

    it("Should accept an empty batch as a no-op", async function () {
      await fileStorage.uploadFile("QmUntouched", "/a.txt", "txt");
      await fileStorage.cleanFolder([]);
      expect(await fileStorage.getFileOwner("QmUntouched")).to.equal(owner.address);
    });
  });

  describe("grantWithExpiry", function () {
    let testCID;

    beforeEach(async function () {
      testCID = "QmTestExpiry";
      await fileStorage.uploadFile(testCID, "test.txt", "text/plain");
    });

    it("Should grant access that is valid before the expiry block", async function () {
      await fileStorage.grantWithExpiry(testCID, user1.address, READ | DOWNLOAD, 5);

      expect(await fileStorage.canRead(testCID, user1.address)).to.be.true;
      expect(Number(await fileStorage.getPermissions(testCID, user1.address))).to.equal(READ | DOWNLOAD);
    });

    it("Should deny access once the expiry block has passed", async function () {
      await fileStorage.grantWithExpiry(testCID, user1.address, READ | DOWNLOAD, 3);

      // mine past the expiry block
      for (let i = 0; i < 5; i++) {
        await ethers.provider.send("evm_mine");
      }

      expect(await fileStorage.canRead(testCID, user1.address)).to.be.false;
      expect(await fileStorage.canDownload(testCID, user1.address)).to.be.false;
      expect(Number(await fileStorage.getPermissions(testCID, user1.address))).to.equal(0);
      expect(await fileStorage.hasAny(testCID, user1.address, READ | DOWNLOAD)).to.be.false;
    });

    it("Should deny access at exactly the expiry block (>=, not >)", async function () {
      const tx = await fileStorage.grantWithExpiry(testCID, user1.address, READ, 2);
      await tx.wait();
      const grantBlock = await ethers.provider.getBlockNumber();

      // mine exactly up to grantBlock + 2
      while ((await ethers.provider.getBlockNumber()) < grantBlock + 2) {
        await ethers.provider.send("evm_mine");
      }

      expect(await fileStorage.canRead(testCID, user1.address)).to.be.false;
    });

    it("Should require durationBlocks > 0", async function () {
      await expect(
        fileStorage.grantWithExpiry(testCID, user1.address, READ, 0)
      ).to.be.revertedWith("Use grant() for permanent access");
    });

    it("Permanent grant() should still work forever regardless of blocks mined", async function () {
      await fileStorage.grant(testCID, user1.address, READ);

      for (let i = 0; i < 10; i++) {
        await ethers.provider.send("evm_mine");
      }

      expect(await fileStorage.canRead(testCID, user1.address)).to.be.true;
      expect(await fileStorage.getExpiresAtBlock(testCID, user1.address)).to.equal(0);
    });

    it("Should restart the clock when re-granting with a new expiry", async function () {
      await fileStorage.grantWithExpiry(testCID, user1.address, READ, 3);
      const firstExpiry = await fileStorage.getExpiresAtBlock(testCID, user1.address);

      await ethers.provider.send("evm_mine");
      await ethers.provider.send("evm_mine");

      // re-share before the first grant expires
      const tx = await fileStorage.grantWithExpiry(testCID, user1.address, READ, 10);
      await tx.wait();
      const secondExpiry = await fileStorage.getExpiresAtBlock(testCID, user1.address);

      expect(secondExpiry).to.be.greaterThan(firstExpiry);
      expect(await fileStorage.canRead(testCID, user1.address)).to.be.true;
    });

    it("A plain grant() after grantWithExpiry should reset the grant to permanent", async function () {
      await fileStorage.grantWithExpiry(testCID, user1.address, READ, 2);
      await fileStorage.grant(testCID, user1.address, READ);

      for (let i = 0; i < 5; i++) {
        await ethers.provider.send("evm_mine");
      }

      expect(await fileStorage.canRead(testCID, user1.address)).to.be.true;
      expect(await fileStorage.getExpiresAtBlock(testCID, user1.address)).to.equal(0);
    });

    it("Should only allow file owner to call grantWithExpiry", async function () {
      await expect(
        fileStorage.connect(user1).grantWithExpiry(testCID, user2.address, READ, 5)
      ).to.be.revertedWith("Not file owner");
    });

    it("Should support batch expiry grants via grantWithExpiryFiles", async function () {
      await fileStorage.uploadFile("QmExpiryB1", "/a.txt", "txt");
      await fileStorage.uploadFile("QmExpiryB2", "/b.txt", "txt");

      await fileStorage.grantWithExpiryFiles(["QmExpiryB1", "QmExpiryB2"], user1.address, READ, 3);
      expect(await fileStorage.canRead("QmExpiryB1", user1.address)).to.be.true;
      expect(await fileStorage.canRead("QmExpiryB2", user1.address)).to.be.true;

      for (let i = 0; i < 5; i++) {
        await ethers.provider.send("evm_mine");
      }

      expect(await fileStorage.canRead("QmExpiryB1", user1.address)).to.be.false;
      expect(await fileStorage.canRead("QmExpiryB2", user1.address)).to.be.false;
    });

    it("Should require durationBlocks > 0 for grantWithExpiryFiles", async function () {
      await expect(
        fileStorage.grantWithExpiryFiles([testCID], user1.address, READ, 0)
      ).to.be.revertedWith("Use grantFiles() for permanent access");
    });

    it("Owner's own access is never subject to expiry", async function () {
      // owner permissions are set directly in _uploadFile, never through _grant
      expect(await fileStorage.getExpiresAtBlock(testCID, owner.address)).to.equal(0);
      for (let i = 0; i < 5; i++) {
        await ethers.provider.send("evm_mine");
      }
      expect(await fileStorage.canRead(testCID, owner.address)).to.be.true;
    });

    it("Should clear expiry data once a user is fully revoked", async function () {
      await fileStorage.grantWithExpiry(testCID, user1.address, READ, 5);
      await fileStorage.revoke(testCID, user1.address, READ);

      expect(await fileStorage.getExpiresAtBlock(testCID, user1.address)).to.equal(0);
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