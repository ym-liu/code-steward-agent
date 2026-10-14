// COMP 202 lab 1
#include <iostream>
#include <string>

int main() {
    std::string name;
    std::cout << "What is your name? ";
    std::getline(std::cin, name);
    std::cout << "Hello, " << name << "!" << std::endl;
    return 0;
}
