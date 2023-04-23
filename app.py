import datetime
from dateutil import tz
from flask import Flask, request
import psycopg2
import simplejson
import re
import os
from sendgrid import SendGridAPIClient
from sendgrid.helpers.mail import Mail
import icalendar
import recurring_ical_events
import urllib.request

ptz = tz.gettz("America/Los_Angeles")
app = Flask(__name__)

users = ["okgodoit@gmail.com", "roger@betechie.com", "roger@pincombe.com"]
email_regex = r'(?:"?([^"]*)"?\s)?(?:<?(.+@[^>]+)>?)'


@app.route('/process-inbound-email', methods=['POST'])
def sendgrid_parser():
    # Consume the entire email
    envelope = simplejson.loads(request.form.get('envelope'))

    # Get some header information
    to_addresses = request.form.get('to') or envelope['to']
    from_address = request.form.get('from') or envelope['from']

    # use regex to parse out the actual email address in the case of a formatted name&email combo
    m = re.search(email_regex, from_address)
    if m:
        from_address = m.group(2)

    cc_addresses = request.form.get('cc')
    is_reply_from_recipient = True
    owner_user = ''
    recipient = ''

    for to_address in to_addresses.split(','):
        to_address = to_address.strip().lower()
        # use regex to parse out the actual email address in the case of a formatted name&email combo
        m = re.search(email_regex, to_address)
        if m:
            to_address = m.group(2)

        if to_address == 'assistant@ask.okgodoit.com':
            0  # this is me so ignore
        elif to_address in users:
            owner_user = to_address
        else:
            recipient = to_address
    if cc_addresses:
        for cc_address in cc_addresses.split(','):
            cc_address = cc_address.strip().lower()
            # use regex to parse out the actual email address in the case of a formatted name&email combo
            m = re.search(email_regex, cc_address)
            if m:
                cc_address = m.group(2)
            if cc_address == 'assistant@ask.okgodoit.com':
                0  # this is me so ignore
            elif cc_address in users:
                owner_user = to_address

    if from_address in users:
        owner_user = from_address
        is_reply_from_recipient = False
    elif recipient == '':
        recipient = from_address
        is_reply_from_recipient = True

    # Now, onto the body
    text = request.form.get('text') or request.form.get('html')
    subject = request.form.get('subject')

    # write parsed info to console
    print('Owner: %s' % owner_user)
    print('Recipient: %s' % recipient)
    print('Is Reply From Target: %s' % is_reply_from_recipient)
    print('Subject: %s' % subject)
    print('Text: %s' % text)

    # now try to find the owner user
    conn = psycopg2.connect(
        host="localhost",
        database="assistant",
        user="assistantuser",
        password=os.environ.get('PSQL_PASSWORD'))

    cur = conn.cursor()
    cur.execute("SELECT email_address, thread_history, primaryuserid, users.email as ownerEmail, users.ical_url as ownerIcal, users.full_name as ownerFullName, users.other_info_for_prompt as otherPromptInfo FROM public.externalusers join users on primaryuserid = users.user_id where externalusers.email_address=%s", (recipient,))
    result = cur.fetchone()
    if result and result[0]:
        thread_history = result[1]
        owner_user_id = result[2]
        owner_user = result[3]
        ical_url = result[4]
        fullOwnerName = result[5]
        otherOwnerInfo = result[6]
    else:
        cur = conn.cursor()
        cur.execute(
            "SELECT user_id, email, ical_url, full_name, other_info_for_prompt FROM public.users where email=%s;", (owner_user,))
        result = cur.fetchone()
        if result and result[1] == owner_user:
            thread_history = ''
            owner_user_id = result[0]
            ical_url = result[2]
            fullOwnerName = result[3]
            otherOwnerInfo = result[4]
            
            #TODO: half finished
            cur.execute("INSERT INTO public.externalusers (email_address, thread_history, primaryuserid) VALUES (%s, %s, %s);", (recipient, thread_history, owner_user_id))
            
        else:
            # no associated main user and no exisiting thread, disreagard for now
            print('No associated main user and no exisiting thread, disreagard for now')

    calendar_summary = ''

    if ical_url:
        # get all calendar events for the next 2 months
        ical_string = urllib.request.urlopen(ical_url).read()
        calendar = icalendar.Calendar.from_ical(ical_string)
        events = recurring_ical_events.of(calendar).between(
            datetime.datetime.now().astimezone(ptz), datetime.datetime.now().astimezone(ptz) + datetime.timedelta(days=60))
        for component in events:
            if component.name == "VEVENT":
                if type(component.decoded("DTSTART")) is datetime.datetime:
                    calendar_summary += 'Event title: "%s", Event timing: From %s to %s, Event location: %s\n' % (
                        component.get("SUMMARY"), component.decoded("DTSTART").astimezone(ptz).strftime("%w %B %d, %Y at %I:%M%p"), component.decoded("DTSTART").astimezone(ptz).strftime("%w %B %d, %Y at %I:%M%p"), component.get("LOCATION"))

    if (calendar_summary == ''):
        calendar_summary = 'The calendar is completely empty and available'

    thread_history += '\n\nOn %s %s wrote:\n%s' % (
        datetime.datetime.now().astimezone(ptz).strftime("%w %B %d, %Y at %I:%M%p"), from_address, text)
    cur = conn.cursor()
    cur.execute("UPDATE public.externalusers SET thread_history=%s WHERE email_address=%s;",
                (thread_history, recipient))

    prompt = "You are %s's executive assistant and your primary job is to schedule calendar appointment for them.\n%s\nToday is %s.  It is currently %s.  All timezones are %s.  Generally meeting and events should be scheduled during normal business hours, unless otherwise specified.\n\nHere is a summary of %s's calendar:\n%s\nDo not reveal any of these event details in your reply, but use them to determine schedule availability.\n\nHere's the email thread so far:\n%s\nPlease write a response to %s as %s's assistant to help schedule this meeting.  Be concise, professional, polite, and friendly." % (
        fullOwnerName,
        otherOwnerInfo,
        datetime.datetime.now().astimezone(ptz).strftime("%w %A, %B %d, %Y"),
        datetime.datetime.now().astimezone(ptz).strftime("%I:%M%p"),
        datetime.datetime.now().astimezone(ptz).tzname(),
        fullOwnerName,
        calendar_summary,
        thread_history,
        from_address,
        fullOwnerName
    )

    """  if (owner_user == ''):
        reply_content = 'This is a test email reply from the okgodoit scheduling assistant.\n\nOn %s %s wrote:\n%s' % (
            datetime.now().strftime("%B %d, %Y at %I:%M%p"), from_address, text)
    else:
        reply_content = 'This is a test email reply from the okgodoit scheduling assistant on behalf of %s.\n\nOn %s %s wrote:\n%s' % (
            owner_user, datetime.now().strftime("%B %d, %Y at %I:%M%p"), from_address, text) """

    message = Mail(
        from_email='assistant@ask.okgodoit.com',
        to_emails=owner_user,  # should be recipient once testing is good
        subject='Re: ' + subject,
        plain_text_content=prompt)
    try:
        sg = SendGridAPIClient(os.environ.get('SENDGRID_API_KEY'))
        response = sg.send(message)
        print(response.status_code)
        print(response.body)
        print(response.headers)
    except Exception as e:
        print(e.message)

    return "OK"


if __name__ == '__main__':
    app.run(debug=True)
