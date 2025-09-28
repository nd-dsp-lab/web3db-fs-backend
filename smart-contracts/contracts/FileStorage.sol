// SPDX-License-Identifier: MIT
pragma solidity ^0.8.28;

contract FileStorage {
    // Store file CIDs/metadata using owner address
    mapping(address => string[]) private userFiles;
    mapping(string => address) private fileOwners;

    // Upload a file and store its IPFS CID -> takes file ID (from IPFS) and adds to user's list
    function uploadFile(string memory cid) public {
        userFiles[msg.sender].push(cid);
        fileOwners[cid] = msg.sender;
    }

    // Getter function to retrieve files for a user based on address
    function getUserFiles(address user) public view returns (string[] memory) {
        return userFiles[user];
    }

    // Check file ownership
    function getFileOwner(string memory cid) public view returns (address) {
        return fileOwners[cid];
    }
}
// Not storing actual files -> that lives on IPFS