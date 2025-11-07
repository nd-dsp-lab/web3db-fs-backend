async function main() {
    console.log("Deploying FileStorage Contract...");
    const Contract = await ethers.getContractFactory("FileStorage");
    const contract = await Contract.deploy();
    await contract.deployed();  // had to update because of different version of Ethers (v5)
    console.log("Contract deployed to:", contract.address); // another version difference (no getAddress function)
}

main()
    .then(() => process.exit(0))
    .catch((error) => {
        console.error(error);
        process.exit(1);
    });