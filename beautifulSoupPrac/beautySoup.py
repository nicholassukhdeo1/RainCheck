from bs4 import BeautifulSoup
import requests

url = 'https://www.scrapethissite.com/pages/forms/'

response = requests.get(url)

soup = BeautifulSoup(response.text, "html.parser") #this'll get all of the website's

print(soup.prettify())
# html