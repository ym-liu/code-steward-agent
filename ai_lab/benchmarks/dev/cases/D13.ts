export interface Item {
  id: string;
  price: number;
}

export declare function loadItems(): Promise<Item[]>;
