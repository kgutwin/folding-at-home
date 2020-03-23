#!/bin/sh

yum -y install https://download.foldingathome.org/releases/public/release/fahclient/centos-6.7-64bit/v7.5/fahclient-7.5.1-1.x86_64.rpm

cat > /etc/fahclient/config.xml <<EOF 
<config><power v='full'/><user v='karlgutwin'/><team v='245750'/><gpu v='true'/>
</config>
EOF
service FAHClient restart

screen -d -m python2 fahstatus.py
