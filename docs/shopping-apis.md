# 구매자 장바구니 API 후보

| 플랫폼 | 공식 흐름 | 연동 범위 |
| --- | --- | --- |
| NHN커머스 샵바이 | 상품·옵션 조회, `POST /cart`, `GET /cart` | 운영하거나 제휴한 샵바이 몰의 클라이언트 ID 및 로그인 회원 세션. 비회원 장바구니는 해당 쇼핑몰의 브라우저 저장소를 사용합니다. |
| 카페24 | Front API의 상품 조회, Front SDK의 `addCart`와 `getCartList` | 입점몰에서 권한을 부여한 앱과 로그인 회원 세션. 앱에 `mall.write_personal` 및 `mall.read_personal` 권한을 설정합니다. |
| Shopify UCP | Global Catalog의 `search_catalog`, 판매처 Cart MCP의 `create_cart`와 `get_cart` | 공개 카탈로그와 지원 판매처를 이용합니다. 한국 배송을 필터링할 수 있고, 장바구니는 판매처마다 생성됩니다. UCP 에이전트 프로필이 필요합니다. |

한국의 자사몰이나 제휴몰이 있다면 샵바이의 공식 API로 구매 흐름을 구성할 수 있습니다. 샵바이는 상품의 판매 옵션 및 수량을 지정해 담고, 로그인 회원의 장바구니 내용을 다시 읽을 수 있습니다. [샵바이 장바구니 API](https://docs.shopby.co.kr/?url.primaryName=order%2F) · [샵바이 호출 가이드](https://workspace-help.nhn-commerce.com/aurora-guide/api/shopbyapi)

카페24의 프런트 SDK도 로그인한 회원의 상품 옵션·수량 추가와 사후 장바구니 조회를 지원합니다. [카페24 Front SDK](https://developers.cafe24.com/app/front/common/frontsdk)

Shopify Global Catalog에 한국 배송 조건을 지정하자 실제 판매 상품이 반환됐습니다. 결과에는 원화와 외화가 함께 표시됐고, 일부 시연 품목은 서로 다른 판매처에 있거나 예산을 초과했습니다. 직템과 나루비타의 공개 UCP 프로필에서 판매처별 장바구니 도구를 확인했습니다. [Shopify Global Catalog](https://shopify.dev/docs/agents/catalog/global-catalog) · [Shopify Cart MCP](https://shopify.dev/docs/agents/carts-and-checkout/cart-mcp) · [직템 UCP](https://jiktem.com/.well-known/ucp) · [나루비타 UCP](https://naruvita.com/.well-known/ucp)
