# 쿠팡 승인 환경 연동

쿠팡과 사전에 확인한 자동화 승인 범위의 문서 식별자와 해당 환경에서 검증한 DOM 정보를 로컬 JSON에 넣는다. 승인에 포함된 비회원 검색·상세·장바구니 화면과 장바구니 비우기·추가 기능을 기준으로 범위를 작성한다. 실행에는 로그인 이력이 없는 전용 Chromium 프로필을 사용한다. 쿠팡 이용약관은 다음에서 확인한다. [쿠팡 이용약관](https://m.coupang.com/nm/policies/terms)

```sh
ZOROCART_CHROMIUM_CHANNEL=chrome python -m zorocart serve --port 8887 \
  --coupang-approval private/approved-scope.json \
  --profile-dir private/coupang-guest
```

대시보드에서 **쿠팡 실사이트 · 비로그인**을 선택한다. 데스크톱 승인 파일에는 `desktop_ui`를 `true`로 설정한다. 동일한 전용 프로필을 사용하는 다음 요청은 같은 비회원 장바구니에서 시작한다. 비회원 화면에서 삭제와 담기 요청 형식을 모두 확인하기 전에는 대시보드에 **쿠팡 비로그인 접속 확인**이 표시된다.

```sh
python -m zorocart run --request '한국어 쇼핑 요청' \
  --coupang-approval private/approved-scope.json \
  --profile-dir private/browser \
  --output logs/coupang-run.json
```

승인 파일과 브라우저 프로필은 로컬에 보관한다. 브라우저 프로필에는 브라우저 세션이 들어갈 수 있다. 승인 파일의 `reference`에는 범위를 확인할 수 있는 문서 식별자를 넣는다.

## 승인 파일

| 키 | 값 |
| --- | --- |
| `reference` | 쿠팡의 사전 승인 또는 협의 기록의 식별자 |
| `desktop_ui` | 현재 데스크톱 화면용 어댑터를 사용하면 `true`. 정확한 변경 요청 형식을 검증할 때까지 변경 설정을 생략하면 조회 전용으로 동작한다. |
| `home_url` | 승인된 쿠팡 HTTPS 상점 시작 URL |
| `cart_url` | 승인된 장바구니 조회 URL. 시작 오리진 또는 `https://cart.coupang.com`을 지원한다. |
| `read_paths` | 시작 오리진에서 승인된 읽기 경로의 정규식 배열. 경로에 전체 일치시키며 검색어 등의 쿼리는 경로 비교에서 제외한다. |
| `cart_read_paths` | 장바구니 오리진에서 승인된 읽기 경로의 정규식 배열. 오리진이 다르고 생략하면 `cart_url`의 경로만 허용한다. |
| `add_path` | 상품과 판매 옵션을 장바구니에 추가하는 단일 POST 경로 |
| `add_origin` | 추가 요청이 발생하는 승인된 시작 또는 장바구니 오리진. 기본값은 시작 오리진이다. |
| `add_fields` | `product`, `option`, `quantity` 세 키를 POST의 대응 필드 이름으로 매핑하는 객체. 평면 JSON과 URL 인코딩 폼을 지원한다. |
| `clear_path` | 기존 장바구니가 있을 때 사용할 승인된 비우기 전용 POST 경로 |
| `clear_origin` | 비우기 요청이 발생하는 승인된 시작 또는 장바구니 오리진. 기본값은 장바구니 오리진이다. |
| `clear_items_field` | 삭제 대상 전체를 전달하는 POST 본문 필드 이름 |
| `asset_hosts` | 필요한 경우에만 승인된 외부 HTTPS 이미지·스타일·스크립트·폰트 호스트를 넣는 배열 |
| `dom` | 승인된 페이지에서 검증한 CSS 셀렉터와 식별자 속성 매핑 |

승인된 화면에 맞는 셀렉터를 직접 구성할 때는 `dom.views`에 `home`, `results`, `detail`, `cart`를 지정한다. `blocked`를 추가하면 승인 환경의 접근 제한 화면을 더 명확히 식별할 수 있다. `dom.controls`에는 `search-input`, `search-submit`, `card`, `option`, `quantity`, `add`, `cart` 키를 넣는다. 상품과 장바구니 링크는 각각 해당 조회 URL에 대한 현재 탭 링크여야 한다.

`dom.fields`는 `name`, `price`, `pack`, `stock`, `shipping`, `shipping-price`, `attributes`, `quantity`, `option`, `category`를 대응하는 표시 요소에 연결한다. 중량·용량·인분 조건이 필요한 경우 `capacity`와 선택적으로 단위 속성 이름인 `unit_attr`를 채운다. 가격, 수량과 구성은 각각 해당 값을 나타내는 요소에 연결한다. 상세는 상품·옵션 컨테이너에서, 장바구니는 상품·옵션별 행에서 값을 읽는다. 현재 구현은 각 옵션의 사양과 가격이 상세 화면에 함께 표시되는 DOM을 지원한다.

표시 가격 영역에 정가와 할인가가 함께 있다면 결제 조건이 없는 현재 가격 요소를 `dom.fields.current-price`에 지정한다. 상품 표시 사양 중 구성 수량을 확인할 요소는 `dom.fields.specs`에 연결한다.

`dom.product`, `dom.variant`, `dom.cart_row`, `dom.empty`, `dom.selected`는 각각 상품 컨테이너, 판매 옵션 컨테이너, 장바구니 상품 행, 빈 장바구니 표시, 선택된 옵션을 식별한다. `dom.key_attr`와 `dom.option_attr`는 화면 요소를 표시 상품 ID와 판매 옵션 ID에 연결하는 속성 이름이다. 검색 카드·상세 상품·장바구니 상품에서 같은 상품 ID를 읽고, 상세 옵션·장바구니 상품에서 같은 판매 옵션 ID를 읽을 수 있어야 한다.

장바구니 비우기가 승인 범위에 포함되면 `clear_path`와 `clear_items_field`를 설정한다. 셀렉터를 직접 구성하는 경우 `dom.controls.clear-cart`에 비우기 버튼을 연결한다. 본문 필드는 `[상품 ID, 판매 옵션 ID, 수량]` 배열의 목록을 받는다. 장바구니 행의 고유 문자열 ID를 받는 방식이면 `dom.clear_row_id_attr`에 해당 속성 이름을 지정한다. JSON 본문은 배열을 보내고 URL 인코딩 폼은 같은 배열을 JSON 문자열로 보낸다. 사용자에게 표시된 삭제 대상과 본문 목록이 일치해야 하며, 승인된 삭제 API의 서버 동작도 같은 대상을 기준으로 검증해야 한다. 장바구니가 비어 있음을 읽기 화면에서 확인한 뒤 계속 진행한다.

조작 전 브라우저는 승인 범위, 현재 관찰 결과, 화면 요소, 옵션·수량·가격을 확인한다. 승인된 장바구니 변경 요청이 확인한 대상과 일치할 때 한 번 전송한다. 로그인, CAPTCHA, 접근 제한, 화면 정보 부족 또는 변경 반영 여부가 불명확하면 확인한 상태를 남기고 중단한다. 쿠팡 DOM과 요청 필드는 승인된 환경에서 검증해야 한다.

승인 범위의 이미 확인한 상품 URL에서 비회원 담기 요청 형식을 조사할 때는 `python -m zorocart.coupang_probe '<상품 URL>' --channel chrome`로 새 브라우저 창을 연다. 진단은 장바구니 변경 요청을 브라우저에서 차단하고 필드의 이름과 형식만 출력한다.
