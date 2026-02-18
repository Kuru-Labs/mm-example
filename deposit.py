#!/usr/bin/env python3
"""
Deposit Helper Script

Deposits tokens into the margin account using direct web3 calls.

Usage:
    # Deposit native token (use 0x0 address)
    python deposit.py --token 0x0000000000000000000000000000000000000000 --amount 1.5

    # Deposit ERC20 token
    python deposit.py --token 0xYourTokenAddress --amount 100.0

    # Check balance only
    python deposit.py --token 0xYourTokenAddress --check
"""

import asyncio
import argparse
import sys
import os
from dotenv import load_dotenv
from web3 import Web3
from eth_account import Account

# Constants
MARGIN_ACCOUNT_ADDRESS = "0x2A68ba1833cDf93fa9Da1EEbd7F46242aD8E90c5"
ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"

MARGIN_ACCOUNT_ABI = [
    {
        "inputs": [
            {"internalType": "address", "name": "_user", "type": "address"},
            {"internalType": "address", "name": "_token", "type": "address"},
            {"internalType": "uint256", "name": "_amount", "type": "uint256"}
        ],
        "name": "deposit",
        "outputs": [],
        "stateMutability": "payable",
        "type": "function"
    },
    {
        "inputs": [
            {"internalType": "address", "name": "_user", "type": "address"},
            {"internalType": "address", "name": "_token", "type": "address"}
        ],
        "name": "getBalance",
        "outputs": [{"internalType": "uint256", "name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function"
    }
]

ERC20_ABI = [
    {
        "inputs": [],
        "name": "decimals",
        "outputs": [{"internalType": "uint8", "name": "", "type": "uint8"}],
        "stateMutability": "view",
        "type": "function"
    },
    {
        "inputs": [],
        "name": "symbol",
        "outputs": [{"internalType": "string", "name": "", "type": "string"}],
        "stateMutability": "view",
        "type": "function"
    },
    {
        "inputs": [{"internalType": "address", "name": "account", "type": "address"}],
        "name": "balanceOf",
        "outputs": [{"internalType": "uint256", "name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function"
    },
    {
        "inputs": [
            {"internalType": "address", "name": "spender", "type": "address"},
            {"internalType": "uint256", "name": "amount", "type": "uint256"}
        ],
        "name": "approve",
        "outputs": [{"internalType": "bool", "name": "", "type": "bool"}],
        "stateMutability": "nonpayable",
        "type": "function"
    },
    {
        "inputs": [
            {"internalType": "address", "name": "owner", "type": "address"},
            {"internalType": "address", "name": "spender", "type": "address"}
        ],
        "name": "allowance",
        "outputs": [{"internalType": "uint256", "name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function"
    }
]


def get_token_info(w3: Web3, token_address: str, user_address: str) -> tuple[int, str, int]:
    """Get token decimals, symbol, and wallet balance."""
    if token_address == ZERO_ADDRESS:
        balance = w3.eth.get_balance(user_address)
        return 18, "ETH", balance

    token = w3.eth.contract(address=Web3.to_checksum_address(token_address), abi=ERC20_ABI)
    decimals = token.functions.decimals().call()
    symbol = token.functions.symbol().call()
    balance = token.functions.balanceOf(user_address).call()
    return decimals, symbol, balance


def get_margin_balance(w3: Web3, margin_contract, user_address: str, token_address: str) -> int:
    """Get margin account balance for a token."""
    return margin_contract.functions.getBalance(
        Web3.to_checksum_address(user_address),
        Web3.to_checksum_address(token_address)
    ).call()


def main():
    parser = argparse.ArgumentParser(
        description='Deposit tokens into margin account',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__
    )
    parser.add_argument('--token', type=str, required=True, help='Token address (use 0x0...0 for native)')
    parser.add_argument('--amount', type=float, help='Amount to deposit (in token units, e.g., 1.5)')
    parser.add_argument('--check', action='store_true', help='Only check balances, do not deposit')

    args = parser.parse_args()

    # Load environment
    load_dotenv()

    private_key = os.getenv("PRIVATE_KEY")
    rpc_url = os.getenv("RPC_URL")

    if not private_key:
        print("Error: PRIVATE_KEY not set in .env")
        sys.exit(1)
    if not rpc_url:
        print("Error: RPC_URL not set in .env")
        sys.exit(1)

    # Connect to RPC
    w3 = Web3(Web3.HTTPProvider(rpc_url))
    if not w3.is_connected():
        print(f"Error: Could not connect to RPC at {rpc_url}")
        sys.exit(1)

    # Get account
    account = Account.from_key(private_key)
    user_address = account.address
    print(f"Connected as: {user_address}")

    # Normalize token address
    token_address = Web3.to_checksum_address(args.token)

    # Get token info
    decimals, symbol, wallet_balance = get_token_info(w3, token_address, user_address)
    wallet_balance_float = wallet_balance / (10 ** decimals)

    # Get margin contract
    margin_contract = w3.eth.contract(
        address=Web3.to_checksum_address(MARGIN_ACCOUNT_ADDRESS),
        abi=MARGIN_ACCOUNT_ABI
    )

    # Get margin balance
    margin_balance = get_margin_balance(w3, margin_contract, user_address, token_address)
    margin_balance_float = margin_balance / (10 ** decimals)

    # Print balances
    print(f"\n{'='*50}")
    print(f"Token: {symbol} ({token_address})")
    print(f"Decimals: {decimals}")
    print(f"{'='*50}")
    print(f"Wallet Balance:  {wallet_balance_float:.6f} {symbol}")
    print(f"Margin Balance:  {margin_balance_float:.6f} {symbol}")
    print(f"{'='*50}\n")

    if args.check:
        return

    if args.amount is None:
        print("Error: --amount required for deposit")
        sys.exit(1)

    amount = args.amount
    amount_wei = int(amount * (10 ** decimals))

    if amount_wei > wallet_balance:
        print(f"Error: Insufficient balance. Have {wallet_balance_float:.6f}, need {amount:.6f}")
        sys.exit(1)

    # For ERC20 tokens, check and set allowance
    if token_address != ZERO_ADDRESS:
        token_contract = w3.eth.contract(address=token_address, abi=ERC20_ABI)
        allowance = token_contract.functions.allowance(user_address, MARGIN_ACCOUNT_ADDRESS).call()

        if allowance < amount_wei:
            print(f"Approving {symbol} for margin account...")
            approve_tx = token_contract.functions.approve(
                MARGIN_ACCOUNT_ADDRESS,
                2**256 - 1  # Max approval
            ).build_transaction({
                'from': user_address,
                'nonce': w3.eth.get_transaction_count(user_address),
                'gas': 100000,
                'gasPrice': w3.eth.gas_price
            })
            signed_approve = account.sign_transaction(approve_tx)
            tx_hash = w3.eth.send_raw_transaction(signed_approve.raw_transaction)
            print(f"Approval TX: {tx_hash.hex()}")
            w3.eth.wait_for_transaction_receipt(tx_hash)
            print("Approval confirmed!")

    # Build deposit transaction
    print(f"Depositing {amount:.6f} {symbol}...")

    tx_params = {
        'from': user_address,
        'nonce': w3.eth.get_transaction_count(user_address),
        'gas': 200000,
        'gasPrice': w3.eth.gas_price
    }

    # For native token, send value with the transaction
    if token_address == ZERO_ADDRESS:
        tx_params['value'] = amount_wei

    deposit_tx = margin_contract.functions.deposit(
        user_address,
        token_address,
        amount_wei
    ).build_transaction(tx_params)

    signed_tx = account.sign_transaction(deposit_tx)
    tx_hash = w3.eth.send_raw_transaction(signed_tx.raw_transaction)
    print(f"Deposit TX: {tx_hash.hex()}")

    receipt = w3.eth.wait_for_transaction_receipt(tx_hash)
    if receipt['status'] == 1:
        print("Deposit confirmed!")
    else:
        print("Deposit failed!")
        sys.exit(1)

    # Show updated balances
    new_wallet_balance = get_token_info(w3, token_address, user_address)[2]
    new_margin_balance = get_margin_balance(w3, margin_contract, user_address, token_address)

    print(f"\n{'='*50}")
    print("UPDATED BALANCES")
    print(f"{'='*50}")
    print(f"Wallet Balance:  {new_wallet_balance / (10**decimals):.6f} {symbol}")
    print(f"Margin Balance:  {new_margin_balance / (10**decimals):.6f} {symbol}")
    print(f"{'='*50}\n")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n\nInterrupted by user")
    except Exception as e:
        print(f"Error: {e}")
        sys.exit(1)
