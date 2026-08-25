from collections import Counter


n = int(input())
a = list(map(int, input().split()))

freq = Counter(a)
candidates = [d for d, c in freq.items() if c >= 2]
candidates.sort(reverse=True)

ans = 0

if len(candidates) >= 2:
    ans = max(ans, candidates[0] * candidates[1])

if candidates and freq[candidates[0]] >= 4:
    ans = max(ans, candidates[0] * candidates[0])

print(ans)

