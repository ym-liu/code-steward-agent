counts = Hash.new(0)
STDIN.each_line do |line|
  key = line.strip
  counts[key] += 1 unless key.empty?
end
counts.sort.each { |key, count| puts "#{key}: #{count}" }
