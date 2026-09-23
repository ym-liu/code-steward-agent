def parse_port(value)
  number = Integer(value, 10)
  raise ArgumentError unless (1..65535).include?(number)
  number
rescue ArgumentError, TypeError
  nil
end
