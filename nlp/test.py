import re

pattern = re.compile(r"(\d+)")
text = "에러 404 and 500 발생"

p = pattern.findall(text)  # → ['404', '500']  문자열 리스트 바로 반환
print(p)

for m in pattern.finditer(text):  # match 객체 하나씩
    print(m)  # → <re.Match object; span=(3,6), match='404'>


HTTP_ERROR_RE = re.compile(r"\b([45]\d{2})\b")
m = HTTP_ERROR_RE.search("500 에러 발생")

print(m.group(0))  # → '500'  (매칭 전체, group() 과 동일)
print(m.group(1))  # → '500'  (첫 번째 캡처 그룹


m = re.compile(r"(\d+)-(\d+)").search("192-168")
print(m.group(0))  # → '192-168'  (전체)
print(m.group(1))  # → '192'      (첫 번째 그룹)
print(m.group(2))  # → '168'      (두 번째 그룹)
