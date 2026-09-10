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

    // cid => user => absolute block number after which access is denied.
    // 0 = no expiry (permanent grant). Matches existing plain grant() behavior
    // since a mapping's default value is 0 -- no migration needed for old grants.
    mapping(string => mapping(address => uint256)) private _expiresAtBlock;

    // can attach this to functions to save time later
    modifier onlyFileOwner(string memory cid) {
        require(msg.sender == fileOwner[cid], "Not file owner");
        _;
    }

    function _grant(string memory cid, address user, uint256 grantMask, uint256 durationBlocks)
        internal
        onlyFileOwner(cid)
    {
        require(user != address(0), "Invalid user");
        // add to sharedFiles if first time getting permissions
        if (_permissions[cid][user] == 0) {
            sharedFiles[user].push(cid);
            sharedUsers[cid].push(user);
        }

        _permissions[cid][user] = _permissions[cid][user] | grantMask;
        // Last grant call always wins on expiry: a fresh grant() call resets
        // the grant to permanent even if a prior grantWithExpiry() had set one;
        // a fresh grantWithExpiry() call always restarts the clock from now.
        _expiresAtBlock[cid][user] = durationBlocks == 0 ? 0 : block.number + durationBlocks;
        emit PermissionGranted(cid, user, grantMask);
    }

    function grant(string memory cid, address user, uint256 grantMask) external {
        _grant(cid, user, grantMask, 0);
    }

    // Batch grant: share many files with one user in one transaction (folder share).
    // Caller must own every cid.
    function grantFiles(string[] memory cids, address user, uint256 grantMask) external {
        for (uint256 i = 0; i < cids.length; i++) {
            _grant(cids[i], user, grantMask, 0);
        }
    }

    // Same as grant, but access is automatically denied once block.number
    // reaches block.number + durationBlocks (computed here, at grant time,
    // so it can't go stale between building and mining the transaction).
    function grantWithExpiry(string memory cid, address user, uint256 grantMask, uint256 durationBlocks) external {
        require(durationBlocks > 0, "Use grant() for permanent access");
        _grant(cid, user, grantMask, durationBlocks);
    }

    // Batch version of grantWithExpiry.
    function grantWithExpiryFiles(string[] memory cids, address user, uint256 grantMask, uint256 durationBlocks) external {
        require(durationBlocks > 0, "Use grantFiles() for permanent access");
        for (uint256 i = 0; i < cids.length; i++) {
            _grant(cids[i], user, grantMask, durationBlocks);
        }
    }

    function _revoke(string memory cid, address user, uint256 revokeMask)
        internal
        onlyFileOwner(cid)
    {
        _permissions[cid][user] = _permissions[cid][user] & ~revokeMask;

        if(_permissions[cid][user] == 0) {
            // storage hygiene only: an expired-but-nonzero _permissions value
            // is already masked to 0 by _effectivePermissions regardless of
            // _expiresAtBlock, so this just avoids leaving stale expiry data.
            delete _expiresAtBlock[cid][user];

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

    function revoke(string memory cid, address user, uint256 revokeMask) external {
        _revoke(cid, user, revokeMask);
    }

    // Batch revoke: unshare many files from one user in one transaction (folder unshare).
    // Caller must own every cid.
    function revokeFiles(string[] memory cids, address user, uint256 revokeMask) external {
        for (uint256 i = 0; i < cids.length; i++) {
            _revoke(cids[i], user, revokeMask);
        }
    }

    // Single choke point for expiry: a live block.number comparison on every
    // read, not a stored flag or a cleanup job. Once expired, this returns 0
    // regardless of what's still sitting in _permissions.
    function _effectivePermissions(string memory cid, address user) internal view returns (uint256) {
        uint256 exp = _expiresAtBlock[cid][user];
        if (exp != 0 && block.number >= exp) {
            return 0;
        }
        return _permissions[cid][user];
    }

    // Permission Query Functions -> from Yanchen
    function getPermissions(string memory cid, address user)
        external
        view
        returns (uint256)
    {
        return _effectivePermissions(cid, user);
    }

    // check if target has same bitmask as requiredMask
    function hasAll(string memory cid, address user, uint256 requiredMask)
        public
        view
        returns (bool)
    {
        return (_effectivePermissions(cid, user) & requiredMask) == requiredMask;
    }

    // check if target has at least 1 bit as anyMask
    function hasAny(string memory cid, address user, uint256 anyMask)
        public
        view
        returns (bool)
    {
        return (_effectivePermissions(cid, user) & anyMask) != 0;
    }

    // 0 = permanent grant (or no grant at all). Frontend/backend converts
    // this to a human ETA using whatever blocks/sec assumption it wants --
    // that conversion intentionally lives entirely outside this contract.
    function getExpiresAtBlock(string memory cid, address user) external view returns (uint256) {
        return _expiresAtBlock[cid][user];
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

    function _uploadFile(string memory cid, string memory filename, string memory fileFormat) internal {
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

    // Upload a file and store its IPFS CID -> takes file ID (from IPFS) and adds to user's list
    function uploadFile(string memory cid, string memory filename, string memory fileFormat) public {
        _uploadFile(cid, filename, fileFormat);
    }

    // Batch upload: register many files in one transaction (folder upload)
    function uploadFiles(string[] memory cids, string[] memory filenames, string[] memory fileFormats) public {
        require(cids.length == filenames.length && cids.length == fileFormats.length, "Length mismatch");
        for (uint256 i = 0; i < cids.length; i++) {
            _uploadFile(cids[i], filenames[i], fileFormats[i]);
        }
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
            delete _expiresAtBlock[cid][user];
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

    // Bulk folder delete: delete every file in the batch the caller owns.
    // Cids the caller does not own are skipped, so a mixed batch never reverts.
    function cleanFolder(string[] memory cids) public {
        for (uint256 k = 0; k < cids.length; k++) {
            if (fileOwner[cids[k]] == msg.sender) {
                deleteFile(cids[k]);
            }
        }
    }
}

// Not storing actual files -> that lives on IPFS