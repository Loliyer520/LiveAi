import requests


class DoubaoSearchService:
    def __init__(
        self,
        api_key: str = '',
        base_url: str = 'https://open.feedcoopapi.com/search_api/global_search',
    ):
        self.api_key = api_key
        self.base_url = base_url

    def with_api_key(self, api_key: str) -> "DoubaoSearchService":
        return DoubaoSearchService(api_key=api_key, base_url=self.base_url)

    def search(self, query: str, doc_count: int = 5, max_snippet_length: int = 500) -> dict:
        query = (query or '').strip()
        if not query:
            raise ValueError('搜索关键词为空')
        if not self.api_key:
            raise RuntimeError('搜索 API Key 未配置')

        headers = {
            'Content-Type': 'application/json',
            'Authorization': f'Bearer {self.api_key}',
        }
        payload = {
            'Query': query,
            'DocCount': doc_count,
            'MaxSnippetLength': max_snippet_length,
            'MaxImageCountPerDoc': 0,
        }
        response = requests.post(self.base_url, headers=headers, json=payload, timeout=20)
        try:
            data = response.json()
        except ValueError as exc:
            raise RuntimeError(f'搜索响应解析失败: {response.text[:200]}') from exc

        error = (data.get('ResponseMetadata') or {}).get('Error')
        if response.status_code != 200 or error:
            raise RuntimeError(f'搜索请求失败: {error or data}')
        return data
