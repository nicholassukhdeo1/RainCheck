import requests

# we will look at the request methods:
# get (to get info)
# put (when u wanna update/change a resource
# delete to delete a resource

params = {
    "name": "Mike",
    "age": 25
}
response = requests.get("https://httpbin.org/get", params=params)
print(response.url)
# we are sending a request to that link.. and getting the
# info from it

# print(response.status_code)
# print(response.text)

res_json = response.json()
del res_json["origin"]
print(res_json)