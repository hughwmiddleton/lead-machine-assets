import sys
from selenium import webdriver
from selenium.webdriver.chrome.options import Options

print("smoke: start", flush=True)
print("smoke: python =", sys.version, flush=True)

opts = Options()
opts.add_argument("--window-size=1400,900")

print("smoke: creating driver...", flush=True)
driver = webdriver.Chrome(options=opts)  # Selenium Manager
print("smoke: driver created", flush=True)

print("smoke: navigating...", flush=True)
driver.get("https://www.facebook.com/")
print("smoke: loaded facebook", flush=True)

input("If Chrome opened, press Enter to quit... ")
driver.quit()
print("smoke: done", flush=True)
