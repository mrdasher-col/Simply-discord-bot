
import random

def create_pasw(length):
    characters = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789!@#$%^&*()_+"
    password = ''.join(random.choice(characters) for _ in range(length))
    return password

f = open('contraseña.txt', 'w', encoding='utf-8')
text = create_pasw(12)
f.write(text)
f.close()

print("Contraseña generada y guardada en 'contraseña.txt':", text)