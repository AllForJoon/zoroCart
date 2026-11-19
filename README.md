# ZoroCart

한국어 요청을 받아 상품을 찾아 장바구니에 담고, 실행 전후의 옵션별 수량 차이를 확인하는 브라우저 에이전트입니다. Zoro-1의 `/v1/chat/completions`로 구매 계획과 검색어를 생성하고 `/v1/systemone`의 Choice와 Score로 품목·상품·화면 조작을 판단합니다.

## 시작하기

Python 3.11 이상을 사용합니다. 기존 팀 Brev 인스턴스의 SSH 권한이 있는 상태에서 실행합니다.

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[test]'
python -m playwright install chromium

ssh -f -N -o ControlMaster=no -o ControlPath=none -o ExitOnForwardFailure=yes \
  -o ServerAliveInterval=30 -L 127.0.0.1:30001:127.0.0.1:30001 l40s-twodrone-instance
python -m zorocart probe
python -m zorocart serve --port 8765
```

[로컬 대시보드](http://127.0.0.1:8765/)에서 자연어 요청을 입력하거나 시연 입력을 선택한 뒤 **장바구니 비우고 시작**을 누릅니다. 유효한 계획이 확정되면 현재 시연 장바구니를 비웁니다. 보이는 Chromium 창에서 합성 상품을 검색하고, 대시보드에 진행 상황과 장바구니 검증 결과를 표시합니다. **시연 장바구니 보기**에서 같은 브라우저 세션의 현재 장바구니를 확인할 수 있습니다. 설치된 Chrome을 쓰려면 `ZOROCART_CHROMIUM_CHANNEL=chrome`을 설정합니다.

팀 Brev에서 확인한 체크포인트는 `bf1b76b9b4df03b05fcbb73444e1cd4db0f566da`, Trida Stack 커밋은 `0ddcf516e458a82892d7fe0d0dabeb113d3cf424`입니다. 팀 서버 가이드의 런타임은 SGLang 0.5.23입니다. 연결 주소와 모델은 `ZORO_URL`, `ZORO_MODEL`로 바꿀 수 있습니다. 기본 임계값은 Score 2.5와 Choice 확률 0.70이고 `ZOROCART_MIN_SCORE`, `ZOROCART_MIN_PROBABILITY`로 설정합니다.

## 실행 로그와 검증

```sh
python -m zorocart run --scenario household --output logs/household.json
python -m zorocart run --scenario pasta --output logs/pasta.json
python -m zorocart run --scenario camping --output logs/camping.json
python -m zorocart validate --runs 5 --output logs/validation
python -m pytest -q
```

`run`은 기본적으로 보이는 Chromium을 사용합니다. `validate`는 기본적으로 헤드리스 Chromium을 사용하며 `--headed`로 창을 표시할 수 있습니다. `validate`는 각 결과를 별도 JSON으로 보존하고, 시나리오별 통과율과 검증된 장바구니 변화 및 소요 시간을 집계합니다. 테스트는 브라우저와 상점을 실제로 구동하며, 예외 재현용 모델 테스트 더블과 Brev 실행 로그를 구분합니다.

브라우저는 화면을 읽고 관찰 ID를 부여한 다음, 모델이 선택한 행동과 요소를 다시 검사합니다. 계획 확정 후 기존 장바구니를 기록하고 비운 다음 빈 상태를 확인합니다. 초기화를 검증하지 못하면 검색과 담기를 중단합니다. 추가 전에는 모든 품목 조합의 예산, 판매 옵션, 재고, 수량을 확인하고 장바구니가 비어 있는지 다시 검사합니다. 추가 후 장바구니를 읽어 옵션별 수량을 대조합니다. 주문·결제 요청은 브라우저 조작 범위에서 차단합니다.

## 쿠팡

승인된 쿠팡 환경은 [쿠팡 연동 설정](docs/coupang.md)을 따릅니다. 합성 상점 검증과 별개로 승인 범위에서 실제 DOM과 장바구니 요청 형식을 확인해야 합니다. 공식 장바구니 API를 지원하는 대안은 [쇼핑 API 후보](docs/shopping-apis.md)에 정리했습니다.

## 네이버플러스 스토어

대시보드에서 **네이버플러스 스토어 · 비로그인**을 선택하면 새 브라우저 세션에서 Zoro-1이 요청을 해석하고 스토어의 검색 화면을 사용합니다. 검색 후 장바구니 접근 여부를 확인합니다. 네이버 로그인 화면이 나타나면 실행을 보류하고 장바구니 초기화와 상품 추가 현황을 표시합니다. [네이버 장바구니 이용 안내](https://help.pay.naver.com/faq/content.help?faqId=11639)

## McGrocer

`serve --mcgrocer-state-dir private/mcgrocer-cart`로 **McGrocer · 비회원** 모드를 열 수 있습니다. Zoro-1은 한국어 요청에서 영어 검색어를 생성하고 공식 Shopify 카탈로그의 후보를 평가합니다. 전용 비회원 장바구니를 비운 뒤 선택한 옵션과 수량을 담고, 장바구니 API로 재조회합니다. 새 Chrome 실행 사이에는 비회원 장바구니 쿠키만 보존합니다. 결과에는 파운드 상품가와 ECB 기준 원화 환산액이 표시됩니다. 한국 배송 가능 여부는 확인되지 않았습니다. [판매처의 UCP 프로필](https://mcgrocer.com/.well-known/ucp) · [Shopify 장바구니 API](https://shopify.dev/docs/api/ajax/reference/cart)

- [Trida Stack decide API](https://github.com/trillion-labs/trida-stack/blob/0ddcf516e458a82892d7fe0d0dabeb113d3cf424/inference/zoro-1/README.md#decisions)
- [Jev Ultrafast 행동 공간](https://github.com/browser-use/jev-ultrafast#the-action-space)
