addEventListener('fetch', event => {
    event.respondWith(handleRequest(event.request))
  })
  
  async function handleRequest(request) {
    // 解析原始请求 URL
    let url = new URL(request.url)
    let newUrl = 'https://raw.githubusercontent.com' + url.pathname + url.search
    
    // 构造一个新的请求，方法、头信息等与原请求保持一致
    let modifiedRequest = new Request(newUrl, {
      method: request.method,
      headers: request.headers,
      body: request.body,
      redirect: 'follow'
    })
    
    // 向 GitHub 发送请求
    let response = await fetch(modifiedRequest)
    
    // 复制响应，方便后续自定义处理（若有需要，比如添加 CORS 头等）
    let newResponse = new Response(response.body, response)
    
    // 你可以根据需要在这里添加/修改/删除响应头
    // 如： newResponse.headers.set('Access-Control-Allow-Origin', '*')
  
    return newResponse
  }
  