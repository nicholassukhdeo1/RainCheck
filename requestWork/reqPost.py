import requests

# we will look at the request methods:
# get (to get info)
# put (when u wanna update/change a resource
# delete to delete a resource
# post: you're sending information to a server — 
    # like submitting a login form or creating an account. 
    # You're not just fetching, you're giving the server data todo 
    # something with.

proxies = {
    "http": "139.99.237.62:80"
}


response = requests.get("http://httpbin.org/get", proxies=proxies)


print(response.text)