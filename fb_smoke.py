from selenium import webdriver
from selenium.webdriver.chrome.options import Options

opts = Options()
opts.add_argument("--window-size=1400,900")
opts.add_argument("--disable-gpu")

driver = webdriver.Remote(
    command_executor="http://127.0.0.1:9515",
    options=opts,
)
driver.get("https://www.facebook.com/")
input("If Chrome opened, press Enter to quit... ")
driver.quit()
