import click
import os
from typing import Annotated  # NOTE: Only Python 3.9+

from ape import chain, Contract
from ape.api import BlockAPI
from ape.exceptions import TransactionError
from ape_aws.accounts import KmsAccount
from ape import plugins
from taskiq import Context, TaskiqDepends, TaskiqState

from silverback import AppState, SilverbackApp


# Do this to initialize your app
app = SilverbackApp()


# Arbitrageur and pool contracts
arbitrageur = Contract(os.environ["CONTRACT_ADDRESS_PAIR_ARBITRAGEUR"])
mrglv1_pool = Contract(os.environ["CONTRACT_ADDRESS_MARGV1_POOL"])
univ3_pool = Contract(mrglv1_pool.oracle())

# Price diff above which execute arbitrage
SQRT_PRICE_TOL = float(
    os.environ.get("SQRT_PRICE_TOLERANCE", 25e-4)
)  # default to > 50 bps in price

# Slippage tolerance for arbitrage (default to 0.5%)
SQRT_PRICE_SLIPPAGE = float(os.environ.get("SQRT_PRICE_SLIPPAGE", 0.005))

AMOUNT_OUT_MIN_ETH = int(float(os.environ.get("AMOUNT_OUT_MIN_ETH", "0.0")) * 10**18)

# Seconds until deadline from last block handled
SECONDS_TIL_DEADLINE = int(os.environ.get("SECONDS_TIL_DEADLINE", 600))  # 10 min

# Gas estimate for the arbitrageur execute function
ARB_GAS_ESTIMATE = int(os.environ.get("ARB_GAS_ESTIMATE", 250000))

# Buffer to add to transaction fee estimate: txn_fee *= 1 + BUFFER
TXN_FEE_BUFFER = float(os.environ.get("TXN_FEE_BUFFER", 0.125))

# Whether to execute transaction through private mempool
TXN_PRIVATE = os.environ.get("TXN_PRIVATE", "False") == "True"

# Required confirmations to wait for transaction to go through
TXN_REQUIRED_CONFIRMATIONS = int(os.environ.get("TXN_REQUIRED_CONFIRMATIONS", 1))

# Whether to ask to enable autosign for local account
PROMPT_AUTOSIGN = app.signer and not isinstance(app.signer, KmsAccount)


# Gets the desired timestamp deadline for arbitrage execution
def _get_deadline(block: BlockAPI, context: Annotated[Context, TaskiqDepends()]):
    return block.timestamp + SECONDS_TIL_DEADLINE


def _get_txn_fee(block: BlockAPI, context: Annotated[Context, TaskiqDepends()]):
    fixed_gas_price = 10 * 10**9  # Set fixed gas price to 10 Gwei
    return int(fixed_gas_price * ARB_GAS_ESTIMATE * (1 + TXN_FEE_BUFFER))


@app.on_startup()
def app_startup(startup_state: AppState):
    # set up autosign if desired
    if PROMPT_AUTOSIGN and click.confirm("Enable autosign?"):
        app.signer.set_autosign(enabled=True)

    return {"message": "Starting...", "block_number": startup_state.last_block_seen}


# Can handle some initialization on startup, like models or network connections
@app.on_worker_startup()
def worker_startup(state: TaskiqState):
    state.block_count = 0
    state.arb_count = 0
    state.signer_balance = app.signer.balance

    # check one of the tokens WETH so can get ETH out
    state.token0 = mrglv1_pool.token0()
    state.token1 = mrglv1_pool.token1()
    state.maintenance = mrglv1_pool.maintenance()
    state.oracle = mrglv1_pool.oracle()
    state.WETH9 = arbitrageur.WETH9()

    if state.token0 != state.WETH9 and state.token1 != state.WETH9:
        raise Exception("One of the tokens in pool must be WETH9")

    # TODO: state.db = MyDB() if allow for tracking many pools
    return {"message": "Worker started."}


@app.on_(chain.blocks)
def exec_block(block: BlockAPI, context: Annotated[Context, TaskiqDepends()]):
    # Execute arbitrage if price differences are beyond tolerance
    univ3_sqrt_price_x96 = univ3_pool.slot0().sqrtPriceX96
    mrglv1_sqrt_price_x96 = mrglv1_pool.state().sqrtPriceX96
    r = univ3_sqrt_price_x96 / mrglv1_sqrt_price_x96 - 1

    click.echo(f"Uniswap v3 sqrt price X96: {univ3_sqrt_price_x96}")
    click.echo(f"Marginal v1 sqrt price X96: {mrglv1_sqrt_price_x96}")
    click.echo(f"Relative difference in sqrt price X96 values: {r}")

    if abs(r) > SQRT_PRICE_TOL:
        amount_out_min = AMOUNT_OUT_MIN_ETH
        deadline = _get_deadline(block, context)
        txn_fee = _get_txn_fee(block, context)

        # Calculate sqrt_price_limit and cap its value
        MAX_UINT160 = 2**160 - 1
        sqrt_price_limit = min(
            univ3_sqrt_price_x96 * (1 + SQRT_PRICE_SLIPPAGE) if r > 0
            else univ3_sqrt_price_x96 * (1 - SQRT_PRICE_SLIPPAGE),
            MAX_UINT160
        )
        sqrt_price_limit = int(sqrt_price_limit)

        # Debugging log
        click.echo(f"Calculated sqrt_price_limit: {sqrt_price_limit}")

        params = [
            context.state.token0,
            context.state.token1,
            context.state.maintenance,
            context.state.oracle,
            app.signer.address,
            context.state.WETH9,
            amount_out_min + txn_fee,
            sqrt_price_limit if r > 0 else 0,  # Dynamic sqrtPriceLimit0
            sqrt_price_limit if r < 0 else 0,  # Dynamic sqrtPriceLimit1
            deadline,
            True,
        ]

        try:
            # Submit the transaction
            click.echo(f"Submitting arbitrage transaction with params: {params}")
            arbitrageur.execute(
                params,
                sender=app.signer,
                required_confirmations=TXN_REQUIRED_CONFIRMATIONS,
                private=TXN_PRIVATE,
                gas=100000
            )

            context.state.arb_count += 1
        except TransactionError as err:
            click.secho(
                f"Transaction error: {err}",
                blink=True,
                bold=True,
            )

    context.state.block_count += 1
    context.state.signer_balance = app.signer.balance
    return {
        "block_count": context.state.block_count,
        "arb_count": context.state.arb_count,
        "signer_balance": context.state.signer_balance,
        "univ3_sqrt_price_x96": univ3_sqrt_price_x96,
        "mrglv1_sqrt_price_x96": mrglv1_sqrt_price_x96,
    }


# Just in case you need to release some resources or something
@app.on_worker_shutdown()
def worker_shutdown(state):
    return {
        "message": f"Worker stopped after handling {state.block_count} blocks.",
        "block_count": state.block_count,
    }


# A final job to execute on Silverback shutdown
@app.on_shutdown()
def app_shutdown():
    return {"message": "Stopping..."}
