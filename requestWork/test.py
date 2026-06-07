# a dictionary is a map

def print_item(item):
   # print("Item:", item["item name"])
   # print("Brand:", item["brand"])
   # print("Size:", item["size"])
    print(f"Item: {item['item name']}")
    print(f"Brand: {item['brand']}")
    print(f"Size: {item['size']}")

my_item = {
    "item name": "Ramones",
    "brand": "Rick Owens",
    "size": 42
}

# print_item(my_item)

pod_shorts = {
    "item name": "Pod Shorts",
    "brand": "Rick Owens",
    "size": 'S'
}

# print_item(pod_shorts)

my_list = []

my_list.append(my_item)
my_list.append(pod_shorts)

for item in my_list:
    print_item(item)

