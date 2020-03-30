#!/bin/sh

FAHUSER=${1:-karlgutwin}
FAHTEAM=${2:-245750}

yum -y install https://download.foldingathome.org/releases/public/release/fahclient/centos-6.7-64bit/v7.5/fahclient-7.5.1-1.x86_64.rpm

cat > /etc/fahclient/config.xml <<EOF 
<config><power v='full'/><user v='$FAHUSER'/><team v='$FAHTEAM'/><gpu v='true'/>
</config>
EOF
service FAHClient restart

python2 -m pip install influxdb-client && screen -d -m python2 fahstatus.py

