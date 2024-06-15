import secrets


def scramble_string(s):
    s_list = list(s)
    secrets.SystemRandom().shuffle(s_list)
    return ''.join(s_list)
