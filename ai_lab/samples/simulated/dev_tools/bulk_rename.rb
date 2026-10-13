#!/usr/bin/env ruby
# Rename files in a folder with a regex.
#   ruby bulk_rename.rb DIR PATTERN REPLACEMENT [--apply]
# Without --apply it only prints what would change.
require 'fileutils'

dir, pattern, replacement = ARGV[0], ARGV[1], ARGV[2]
abort 'usage: bulk_rename.rb DIR PATTERN REPLACEMENT [--apply]' if [dir, pattern, replacement].any?(&:nil?)
apply = ARGV.include?('--apply')
regex = Regexp.new(pattern)

Dir.children(dir).sort.each do |name|
  next unless name.match?(regex)
  new_name = name.gsub(regex, replacement)
  next if new_name == name
  puts "#{name} -> #{new_name}"
  FileUtils.mv(File.join(dir, name), File.join(dir, new_name)) if apply
end
