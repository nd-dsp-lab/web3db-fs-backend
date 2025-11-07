// SPDX-License-Identifier: MIT
pragma solidity ^0.8.28;

contract FileStorage {
    // File metadata structure
    struct FileMetadata {
        string cid;
        string filename;
        string folderPath;
        uint256 timestamp;
    }


    // Store file CIDs/metadata using owner address
    mapping(address => FileMetadata[]) private userFiles;
    mapping(string => address) private fileOwners;

    // Shared files mapping
    mapping(address => FileMetadata[]) private sharedFiles;

    // Track what addresses a CID has been shared with
    mapping(string => address[]) private sharedWithList;
    mapping(string => mapping(address => uint256)) private sharedFileIndex; // for unsharing files

    event FileUploaded(address indexed owner, string cid);
    event FileShared(address indexed owner, address indexed to, string cid);
    event UnsharedFile(address indexed owner, address indexed from, string cid);
    // Note for my understanding:
    // An "event" in solidity is a way for smart contract to log data on the blockchain
    // Cheaper than actually storing the data

    // Upload a file and store its IPFS CID -> takes file ID (from IPFS) and adds to user's list
    function uploadFile(string memory cid, string memory filename, string memory folderPath) public {
        FileMetadata memory newFile = FileMetadata({
            cid: cid,
            filename: filename,
            folderPath: folderPath,
            timestamp: block.timestamp
        });

        userFiles[msg.sender].push(newFile);
        fileOwners[cid] = msg.sender;

        emit FileUploaded(msg.sender, cid);
    }

    // Share a file with another user -> only owner can share
    function shareFile(string memory cid, address to_user) public {
        require(to_user != address(0), "Invalid recipient");
        address owner = fileOwners[cid];
        require(owner == msg.sender, "Have to be owner to share");

        // ensure not already shared
        require(sharedFileIndex[cid][to_user] == 0, "File already shared");

        // make sure user can't share with self
        require(to_user != owner, "Cannot share with self");

        // find metadata in owner's list
        FileMetadata memory meta = _findOwnerFileMeta(owner, cid);

        // record sharing state
        sharedFiles[to_user].push(meta);
        sharedWithList[cid].push(to_user);
        sharedFileIndex[cid][to_user] = sharedWithList[cid].length; // store i + 1

        emit FileShared(owner, to_user, cid);
    }

    // Unshare a file from user -> only owner can unshare
    function unshareFile(string memory cid, address from_user) public {
        address owner = fileOwners[cid];
        require(owner == msg.sender, "Have to be owner to unshare");

        uint256 iPlusOne = sharedFileIndex[cid][from_user];
        require(iPlusOne != 0, "File not shared with this user");

        // remove from sharedWithList
        uint256 i = iPlusOne - 1;
        uint256 prev = sharedWithList[cid].length - 1;
        if (i != prev) {
            address prevAddr = sharedWithList[cid][prev];
            sharedWithList[cid][i] = prevAddr;
            sharedFileIndex[cid][prevAddr] = i + 1;
        }
        sharedWithList[cid].pop();
        sharedFileIndex[cid][from_user] = 0;

        // get rid of FileMetadata from from_user's sharedFiles
        uint256 files_len = sharedFiles[from_user].length;
        for (uint256 j = 0; j < files_len; j++) {
            // keccak is the standard way to compare strings in solidity (essentially a hash)
            if (keccak256(bytes(sharedFiles[from_user][j].cid)) == keccak256(bytes(cid))) {
                if (i != files_len - 1) {
                    sharedFiles[from_user][j] = sharedFiles[from_user][files_len - 1];
                }
                sharedFiles[from_user].pop();
                break;
            }
        }

        emit UnsharedFile(owner, from_user, cid);
    }

    // Getter function to retrieve files for a user based on address -> updated for both owned and shared files
    function getUserFiles(address user) public view returns (FileMetadata[] memory) {
        uint256 ownFilesLen = userFiles[user].length;
        uint256 sharedFilesLen = sharedFiles[user].length;
        FileMetadata[] memory allFiles = new FileMetadata[](ownFilesLen + sharedFilesLen);
        for (uint256 i = 0; i < ownFilesLen; i++) {
            allFiles[i] = userFiles[user][i];
        }
        for (uint256 j = 0; j < sharedFilesLen; j++) {
            allFiles[ownFilesLen + j] = sharedFiles[user][j];
        }

        return allFiles;
    }

    // Check file ownership
    function getFileOwner(string memory cid) public view returns (address) {
        return fileOwners[cid];
    }

    // Helper to find metadata (in internal memory, not blockchain)
    function _findOwnerFileMeta(address owner, string memory cid) internal view returns (FileMetadata memory) {
        uint256 len = userFiles[owner].length;
        for (uint256 i= 0; i < len; i++) {
            if (keccak256(bytes(userFiles[owner][i].cid)) == keccak256(bytes(cid))) {
                return userFiles[owner][i];
            }
        }
        revert("File not found for owner");
    }
}
// Not storing actual files -> that lives on IPFS