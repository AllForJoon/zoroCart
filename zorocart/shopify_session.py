"""Preserve only the first-party cart from the app's anonymous McGrocer session."""

from pathlib import Path
import re
from tempfile import NamedTemporaryFile

from .shopify_live import MCGROCER_URL, ShopifyError

_VALUE = re.compile(r'[!-~]{1,1024}\Z')


async def restore_cart(context, state_dir: Path) -> None:
    state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    state = state_dir / 'cart-session'
    if not state.exists():
        return
    value = state.read_text(encoding='ascii')
    if not _VALUE.fullmatch(value):
        raise ShopifyError('저장된 McGrocer 비회원 장바구니 세션을 확인할 수 없습니다.')
    await context.add_cookies([{'name': 'cart', 'value': value, 'url': MCGROCER_URL, 'secure': True}])


async def save_cart(context, state_dir: Path) -> None:
    cookies = await context.cookies(MCGROCER_URL)
    cart = [cookie['value'] for cookie in cookies if cookie['name'] == 'cart' and cookie['path'] == '/'
            and cookie['domain'].lstrip('.') == 'mcgrocer.com' and _VALUE.fullmatch(cookie['value'])]
    if not cart:
        return
    if len(cart) != 1:
        raise ShopifyError('McGrocer 비회원 장바구니 세션을 구분할 수 없습니다.')
    with NamedTemporaryFile(mode='w', encoding='ascii', dir=state_dir, delete=False) as handle:
        destination = Path(handle.name)
        try:
            handle.write(cart[0])
            handle.flush()
            destination.replace(state_dir / 'cart-session')
        finally:
            destination.unlink(missing_ok=True)
