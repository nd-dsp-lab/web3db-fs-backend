// SPDX-License-Identifier: MIT
pragma solidity ^0.8.28;

contract FileStorage {
    // File metadata structure
    struct FileMetadata {
        string cid;
        string filename;    // filename is going to be the folderPath
        // string folderPath;
        string fileFormat;
        uint256 timestamp;
    }

    // From Yanchen doc
    uint public constant READ               = 1 << 0;   // 0x01
    uint public constant WRITE              = 1 << 1;   // 0x02
    uint public constant DOWNLOAD           = 1 << 2;   // 0x04
    uint public constant DELETE             = 1 << 3;   // 0x08
    uint public constant SHARE              = 1 << 4;   // 0x10
    uint public constant MOVE               = 1 << 5;   // 0x20
    uint public constant CHANGE_OWNER       = 1 << 6;   // 0x40
    uint public constant CHANGE_ROLE        = 1 << 7;   // 0x80

    mapping(string => FileMetadata) public fileMetadata;

    // Store file CIDs/metadata using owner address
    mapping(address => string[]) private sharedFiles;   // files a user can "access" -> shared with
    mapping(address => FileMetadata[]) private userFiles; // files a user owns
    mapping(string => address[]) private sharedUsers;   // mapping file cid to users with access to that file (for deleting)    

    event FileUploaded(address indexed owner, string cid);
    event FileDeleted(address indexed owner, string cid);
    event PermissionGranted(string indexed cid, address indexed user, uint256 permissions);
    event PermissionRevoked(string indexed cid, address indexed user, uint256 permissions);
    event FileMoved(address indexed owner, string cid, string newPath);

    // CID => owner address
    mapping(string => address) public fileOwner;

    // CID => (user address => permission bitmask)
    mapping(string => mapping(address => uint256)) private _permissions;

    // can attach this to functions to save time later
    modifier onlyFileOwner(string memory cid) {
        require(msg.sender == fileOwner[cid], "Not file owner");
        _;
    }

    // Core permission functions -> from Yanchen
    function setPermissions(string memory cid, address user, uint256 newPermissions)
        external
        onlyFileOwner(cid)
    {
        require(user != address(0), "Invalid user");
        _permissions[cid][user] = newPermissions;
        emit PermissionGranted(cid, user, newPermissions);
    }

    function grant(string memory cid, address user, uint256 grantMask)
        external
        onlyFileOwner(cid)
    {
        // add to sharedFiles if first time getting permissions
        if (_permissions[cid][user] == 0) {
            sharedFiles[user].push(cid);
            sharedUsers[cid].push(user);
        }

        _permissions[cid][user] = _permissions[cid][user] | grantMask;
        emit PermissionGranted(cid, user, grantMask);
    }

    function revoke(string memory cid, address user, uint256 revokeMask)
        external
        onlyFileOwner(cid)
    {
        _permissions[cid][user] = _permissions[cid][user] & ~revokeMask;

        if(_permissions[cid][user] == 0) {
            // remove cid from sharedFiles[user]
            string[] storage files = sharedFiles[user];
            uint256 ulen = files.length;
            for (uint256 j = 0; j < ulen; j++) {
                if (keccak256(bytes(files[j])) == keccak256(bytes(cid))) {
                    if (j != ulen - 1) {
                        files[j] = files[ulen - 1];
                    }
                    files.pop();
                    break;
                }
            }

            // remove user from sharedUsers[cid]
            address[] storage users = sharedUsers[cid];
            uint256 slen = users.length;
            for (uint256 i = 0; i < slen; i++) {
                if (users[i] == user) {
                    if (i != slen - 1) users[i] = users[slen - 1];
                    users.pop();
                    break;
                }
            }
        }
        
        emit PermissionRevoked(cid, user, revokeMask);
    }

    // Permission Query Functions -> from Yanchen
    function getPermissions(string memory cid, address user)
        external
        view
        returns (uint256)
    {
        return _permissions[cid][user];
    }

    // check if target has same bitmask as requiredMask
    function hasAll(string memory cid, address user, uint256 requiredMask)
        public
        view
        returns (bool)
    {
        return (_permissions[cid][user] & requiredMask) == requiredMask;
    }

    // check if target has at least 1 bit as anyMask
    function hasAny(string memory cid, address user, uint256 anyMask)
        public
        view
        returns (bool)
    {
        return (_permissions[cid][user] & anyMask) != 0;
    }

    // Permission helper functions (return if a user has a certain permission for a given file)
    function canRead(string memory cid, address user) external view returns (bool) {
        return hasAll(cid, user, READ);
    }

    function canWrite(string memory cid, address user) external view returns (bool) {
        return hasAll(cid, user, WRITE);
    }

    function canDownload(string memory cid, address user) external view returns (bool) {
        return hasAll(cid, user, DOWNLOAD);
    }

    function canDelete(string memory cid, address user) external view returns (bool) {
        return hasAll(cid, user, DELETE);
    }

    function canShare(string memory cid, address user) external view returns (bool) {
        return hasAll(cid, user, SHARE);
    }

    function canMove(string memory cid, address user) external view returns (bool) {
        return hasAll(cid, user, MOVE);
    }

    function canChangeOwner(string memory cid, address user) external view returns (bool) {
        return hasAll(cid, user, CHANGE_OWNER);
    }

    function canChangeRole(string memory cid, address user) external view returns (bool) {
        return hasAll(cid, user, CHANGE_ROLE);
    }

    // Upload a file and store its IPFS CID -> takes file ID (from IPFS) and adds to user's list
    function uploadFile(string memory cid, string memory filename, string memory fileFormat) public {
        require(fileOwner[cid] == address(0), "File already exists");
        
        FileMetadata memory newFile = FileMetadata({
            cid: cid,
            filename: filename,
            fileFormat: fileFormat,
            timestamp: block.timestamp
        });

        fileMetadata[cid] = newFile;
        userFiles[msg.sender].push(newFile);
        fileOwner[cid] = msg.sender;
        _permissions[cid][msg.sender] = 0xFF;

        emit FileUploaded(msg.sender, cid);
    }

    // Getter function to retrieve files for a user based on address -> updated for both owned and shared files
    function getUserFiles(address user) public view returns (FileMetadata[] memory) {
        string[] memory sharedCIDs = sharedFiles[user];
        uint256 ownedCount = userFiles[user].length;
        uint256 sharedCount = sharedCIDs.length;
        uint256 numFiles = ownedCount + sharedCount;
        
        FileMetadata[] memory allFiles = new FileMetadata[](numFiles);

        // owned files 
        for (uint256 i = 0; i < userFiles[user].length; i++) {
            allFiles[i] = userFiles[user][i];
        }

        // shared
        for (uint256 j = 0; j < sharedFiles[user].length; j++) {
            allFiles[ownedCount + j] = fileMetadata[sharedCIDs[j]];
        }

        return allFiles;
    }

    // Check file ownership
    function getFileOwner(string memory cid) public view returns (address) {
        return fileOwner[cid];
    }

    // Getter to retrieve shared users
    function getSharedUsers(string memory cid) public view returns (address[] memory) {
        return sharedUsers[cid];
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

    // Deletes file metadata from owner's list, gets rid of fileOwner mapping, and unshares from anyone (only owner can delete)
    function deleteFile(string memory cid) public onlyFileOwner(cid) {
        address owner = msg.sender; // has to be owner because of modifier

        // remove from owner's userFiles -> the files mapped to their address
        uint256 len = userFiles[owner].length;
        for (uint256 i = 0; i < len; i++) {
            if (keccak256(bytes(userFiles[owner][i].cid)) == keccak256(bytes(cid))) {
                if (i != len - 1) {
                    userFiles[owner][i] = userFiles[owner][len - 1];
                }
                userFiles[owner].pop();
                break;
            }
        }
        // remove from all users' sharedFiles list
        address[] memory sharedWith = sharedUsers[cid];
        for (uint256 i = 0; i < sharedWith.length; i++) {
            address user = sharedWith[i];
            string[] storage files = sharedFiles[user];

            // removes particular cid from user's sharedFiles array
            uint256 ulen = files.length;
            for (uint256 j = 0; j < ulen; j++) {
                if (keccak256(bytes(files[j])) == keccak256(bytes(cid))) {
                    if (j != ulen - 1) {
                        files[j] = files[ulen - 1];
                    }
                    files.pop();
                    break;
                }
            }

            delete _permissions[cid][user]; // clearing permissions per user
        }


        delete fileMetadata[cid];
        delete fileOwner[cid];

        // emit event
        emit FileDeleted(owner, cid);
    }

    function _moveFile(string memory cid, string memory newPath) internal {
        require(msg.sender == fileOwner[cid], "Not file owner");

        // Update the metadata mapping
        fileMetadata[cid].filename = newPath;

        // Update the copy stored in the owner's userFiles array
        address owner = msg.sender;
        uint256 len = userFiles[owner].length;
        for (uint256 i = 0; i < len; i++) {
            if (keccak256(bytes(userFiles[owner][i].cid)) == keccak256(bytes(cid))) {
                userFiles[owner][i].filename = newPath;
                break;
            }
        }

        emit FileMoved(owner, cid, newPath);
    }

    function moveFile(string memory cid, string memory newPath) public {
        _moveFile(cid, newPath);
    }

    // Batch move: rename/move many files in one transaction (folder rename,
    // bulk trash/restore). Caller must own every cid.
    function moveFiles(string[] memory cids, string[] memory newPaths) public {
        require(cids.length == newPaths.length, "Length mismatch");
        for (uint256 i = 0; i < cids.length; i++) {
            _moveFile(cids[i], newPaths[i]);
        }
    }

    //Delete folders and file metadata from owners list
   function cleanFolder(string[] memory cids)public {
        address user = msg.sender;
        for (uint256 k =0; k<cids.length; k++){
            string memory cid = cids[k];

            if (fileOwner[cid] == user){
                deleteFile(cid);
            } else {
                FileMetadata[] storage files = userFiles[user];
                for (uint256 i = 0; i< files.length; i ++){
                    if (keccak256(bytes(files[i].cid)) == keccak256(bytes(cid))) {
                        if (i != files.length - 1) {
                            files[i] = files[files.length - 1];
                        }
                        files.pop();
                        break;
                    }
                }
            }
        }
   }
}

// Not storing actual files -> that lives on IPFS