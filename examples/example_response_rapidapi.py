import http.client

conn = http.client.HTTPSConnection("instagram120.p.rapidapi.com")

payload = "{\"username\":\"aynura_aghayeva\",\"maxId\":\"\"}"

headers = {
    'x-rapidapi-key': "e6b1720a0dmsh29b883606ae5766p101fa8jsn292cf87da923",
    'x-rapidapi-host': "instagram120.p.rapidapi.com",
    'Content-Type': "application/json"
}

conn.request("POST", "/api/instagram/posts", payload, headers)

res = conn.getresponse()
data = res.read()

print(data.decode("utf-8"))