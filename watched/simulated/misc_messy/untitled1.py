import numpy as np

a = np.random.rand(5, 5)
print(a @ a.T)

# does broadcasting work here??
b = np.arange(5)
print(a + b)
print((a + b[:, None]).shape)

x = [i**2 for i in range(10)]
print(sum(x), max(x))
