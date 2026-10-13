type User = {id: string; enabled: boolean};

export function activeIds(users: User[]): string[] {
  return users.filter(user => user.enabled === true).map(user => user.id);
}
