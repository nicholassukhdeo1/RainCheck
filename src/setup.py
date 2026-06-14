
# bad idea to have this import in there because it runs whatevers in the main of
# yahoo_auc.. because that stuff isnt in any function.

# from yahoo_auc_scrape import yahoo_jp_scrape
import json

stop = ""


# this try/exception block basically refreshes what we alr
# have in this variable item_list each time we run it.
try:
    with open("catalog.json", "r") as f:
        item_list = json.load(f)
except FileNotFoundError:
    # first run — no file exists yet, so start fresh
    item_list = []

while stop != "done":
    
    desired_item = input("Tell me something you'd like to get daily updates on!\n")

    # you need to convert this into a float.. cuz whatever you take via
    # input will be a string. but you need to work w/ a float for this.
    item_price = float(input("What's the market price on it?\n"))

    user_item = {
        "item name": desired_item,
        "brand": "Rick Owens",
        "size": 42,

        # so, it modifies what we put via desired_item by taking
        # any space you inputted in, and turning it into a +
        # because thats how these search links are built in first place
        "search_query": desired_item.replace(" ", "+"),
        "market_price": item_price,
        "keywords": ["ramones", "ramon", "high top"]
    }

    item_list.append(user_item)


    stop = input("Are you done adding items? Type 'done' if so, or 'continue' if not.\n")


with open("catalog.json", "w") as f:
    json.dump(item_list, f, indent=4)
