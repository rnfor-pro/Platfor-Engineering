#!/bin/bash
echo "Please enter any filename: "
read name
touch $name
echo "Please enter any directory name: "
read dir 
mkdir $dir 
cp $name $dir 